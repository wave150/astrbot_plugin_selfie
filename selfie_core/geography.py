"""Framework-free home/travel configuration and city-scoped geography.

CityCatalog.from_dict consumes an already decoded JSON object, not a file:
{city: {"districts": {district: {"lat": 34.3, "lon": 108.9,
                              "neighbors": [other_district]}},
        "stations": [district]}}
Neighbors/stations default to empty lists; all references must stay in that
city. Neighbor edges remain directed as supplied. No station names, external
routes, network lookup, implicit coordinates or geographic guesses are added.
has_route checks only distinct configured cities with station districts at both
ends, not real timetables or connectivity within a city's district graph.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType


def _name(value, label, empty=False):
    if value is None and empty:
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{label} must be a string without NUL")
    value = value.strip()
    if not value and not empty:
        raise ValueError(f"{label} must not be empty")
    return value


def _names(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of nonempty strings")
    return tuple(dict.fromkeys(_name(v, label) for v in value))


@dataclass(frozen=True)
class WorldProfile:
    home_city: str
    home_district: str
    travel_cities: tuple[str, ...]

    @property
    def configured(self) -> bool:
        """A world is only usable once the administrator picked a home city."""
        return bool(self.home_city)

    @classmethod
    def from_config(cls, config=None, legacy_home_city="",
                    legacy_home_district="", legacy_travel_city=""):
        """Empty settings mean "not configured yet"; nothing is assumed.

        The plugin ships no built-in city, so an unconfigured profile has empty
        home city/district and no travel destinations. A changed home city
        requires its district explicitly, preventing the old home's district
        from being silently attached to an unrelated new city. Travel cities are
        trimmed, deduplicated and exclude the home city.
        """
        config = {} if config is None else config
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping or None")
        raw = config.get("travel_cities")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            legacy = _name(legacy_travel_city, "legacy_travel_city", empty=True)
            destinations = (legacy,) if legacy else ()
        else:
            # Validate the shape even before a home city exists, so a malformed
            # setting never silently looks "fine".
            destinations = _names(raw, "travel_cities")
        legacy_home_city = _name(legacy_home_city, "legacy_home_city", empty=True)
        legacy_home_district = _name(legacy_home_district, "legacy_home_district", empty=True)
        city = _name(config.get("home_city"), "home_city", empty=True) or legacy_home_city
        district = _name(config.get("home_district"), "home_district", empty=True)
        if not city:
            if district:
                raise ValueError("home_city is required when home_district is set")
            return cls("", "", ())
        district = district or (legacy_home_district if city == legacy_home_city else "")
        if not district:
            raise ValueError("home_district is required when home_city is set")
        return cls(city, district, tuple(c for c in destinations if c != city))


@dataclass(frozen=True)
class _District:
    lat: float
    lon: float
    neighbors: tuple[str, ...]


@dataclass(frozen=True)
class _City:
    districts: Mapping
    stations: tuple[str, ...]


def _coordinate(value, bound, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number) or not -bound <= number <= bound:
        raise ValueError(f"{label} must be within [-{bound}, {bound}]")
    return number


@dataclass(frozen=True)
class CityCatalog:
    """Immutable parsed snapshot; lookups never fall back to another city."""

    _cities: Mapping

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, Mapping):
            raise ValueError("city catalog must be an object mapping cities to definitions")
        cities = {}
        for raw_city, definition in data.items():
            city = _name(raw_city, "city")
            if city in cities:
                raise ValueError(f"duplicate normalized city: {city}")
            if not isinstance(definition, Mapping):
                raise ValueError(f"city {city} must be an object")
            raw_districts = definition.get("districts")
            if not isinstance(raw_districts, Mapping) or not raw_districts:
                raise ValueError(f"city {city} must define nonempty districts")
            districts = {}
            for raw_district, entry in raw_districts.items():
                district = _name(raw_district, f"district in {city}")
                if district in districts:
                    raise ValueError(f"duplicate normalized district in {city}: {district}")
                if not isinstance(entry, Mapping):
                    raise ValueError(f"district {city}/{district} must be an object")
                lat = _coordinate(entry.get("lat"), 90, f"{city}/{district}.lat")
                lon = _coordinate(entry.get("lon"), 180, f"{city}/{district}.lon")
                neighbors = _names(entry.get("neighbors", []), f"{city}/{district}.neighbors")
                districts[district] = _District(lat, lon, neighbors)
            stations = _names(definition.get("stations", []), f"{city}.stations")
            for district, entry in districts.items():
                for neighbor in entry.neighbors:
                    if neighbor not in districts:
                        raise ValueError(f"unknown neighbor {neighbor} in {city}/{district}")
            for station in stations:
                if station not in districts:
                    raise ValueError(f"unknown station district {station} in {city}")
            cities[city] = _City(MappingProxyType(districts), stations)
        return cls(MappingProxyType(cities))

    def _city(self, city):
        city = _name(city, "city")
        if city not in self._cities:
            raise ValueError(f"unknown city: {city}")
        return self._cities[city]

    def _district(self, city, district):
        record = self._city(city)
        district = _name(district, "district")
        if district not in record.districts:
            raise ValueError(f"unknown district {district} in {city}")
        return record.districts[district]

    def city_names(self):
        return tuple(self._cities)

    def district_names(self, city):
        return tuple(self._city(city).districts)

    def coords(self, city, district):
        entry = self._district(city, district)
        return entry.lat, entry.lon

    def neighbors(self, city, district):
        return self._district(city, district).neighbors

    def station_districts(self, city):
        return self._city(city).stations

    def has_route(self, from_city, to_city):
        """False for unknown/same cities or missing station endpoints."""
        if from_city not in self._cities or to_city not in self._cities or from_city == to_city:
            return False
        return bool(self._cities[from_city].stations and self._cities[to_city].stations)

    def validate_profile(self, profile, require_routes=False):
        """Raise ValueError on missing home/destinations (and optionally routes)."""
        if not isinstance(profile, WorldProfile):
            raise ValueError("profile must be a WorldProfile")
        if not profile.configured:
            return
        self._district(profile.home_city, profile.home_district)
        for city in profile.travel_cities:
            self._city(city)
            if require_routes and not self.has_route(profile.home_city, city):
                raise ValueError(f"no station route from {profile.home_city} to {city}")
