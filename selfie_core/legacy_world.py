"""One-time import of pre-workspace world data (``districts.txt`` / ``scenes.txt``).

The plugin no longer ships any city data: a fresh install starts with an empty
world and the administrator picks a home city in the workspace, which generates
that city. These files only exist in instances created before the world moved
into ``workspace.json``; when they are present they are converted into ordinary
workspace city records so an upgraded instance keeps its own places.

Nothing here is used at runtime after the import: the workspace is the single
source of truth for cities, district topology and places.
"""
from __future__ import annotations

import re
from pathlib import Path

from .defaults import DISTRICT_FILENAME, SCENE_FILENAME, STATION_PLACE_HINTS


def _split(text: str) -> list[str]:
    return [part.strip() for part in str(text or "").split("|")]


def _neighbors(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,，、/]+", value or "") if x.strip()]


def _read_lines(path: Path):
    with path.open(encoding="utf-8-sig") as stream:
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            yield line


def read_districts(path: Path) -> dict[str, dict]:
    """``区名 | 纬度 | 经度 | 相邻区`` -> {district: {lat, lon, neighbors}}."""
    table: dict[str, dict] = {}
    if not path.is_file():
        return table
    for line in _read_lines(path):
        parts = _split(line)
        if len(parts) < 3:
            continue
        try:
            lat, lon = float(parts[1]), float(parts[2])
        except ValueError:
            continue
        table[parts[0]] = {
            "lat": lat,
            "lon": lon,
            "neighbors": _neighbors(parts[3]) if len(parts) >= 4 else [],
        }
    return table


def read_places(path: Path, default_city: str = "") -> list[dict]:
    """``地点 | 类型 | 活动名 | 描述 | 所在区 | 城市`` -> activity rows.

    描述里的 ``|`` 归描述、最后两列才是区与城市；旧实现把描述后面的区/城市
    也拼进了描述（出现「…步道|未央区|西安」），升级后由 workspace 的
    一次性清理把已导入的行修回来。
    """
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in _read_lines(path):
        parts = _split(line)
        head = parts[0]
        if head in ("地点", "活动"):
            if len(parts) < 4:
                continue
            kind = parts[1]
            name = parts[2]
            if len(parts) >= 6:
                place = "|".join(parts[3:-2]) or parts[3]
                district, city = parts[-2], parts[-1]
            elif len(parts) == 5:
                place = "|".join(parts[3:-1]) or parts[3]
                district, city = parts[-1], default_city
            else:  # 只有 4 列：没有区和城市
                place = parts[3]
                district, city = "", default_city
            if not (kind and place):
                continue
            rows.append({
                "kind": kind, "name": name, "place": place,
                "district": district, "city": city or default_city,
            })
        # ``文案`` rows only override generic templates; the plugin now keeps
        # those templates in code, so they are intentionally skipped.
    return rows


def components(table: dict[str, dict]) -> list[set[str]]:
    """Connected district groups, so one flat file can describe several cities."""
    seen: set[str] = set()
    groups: list[set[str]] = []
    for start in table:
        if start in seen:
            continue
        group = {start}
        queue = [start]
        seen.add(start)
        while queue:
            name = queue.pop()
            related = set(table.get(name, {}).get("neighbors") or ())
            for other, entry in table.items():
                if name in (entry.get("neighbors") or ()):
                    related.add(other)
            for other in related:
                if other in table and other not in seen:
                    seen.add(other)
                    group.add(other)
                    queue.append(other)
        groups.append(group)
    return groups


def _is_station(row: dict) -> bool:
    text = str(row.get("name") or "") + str(row.get("place") or "")
    return any(hint in text for hint in STATION_PLACE_HINTS)


def import_legacy(plugin_dir, default_city: str = "") -> dict[str, dict]:
    """Build workspace city records from the legacy files, or {} if absent.

    Cities are inferred only from the data itself: district groups come from the
    neighbour graph, and each group is named after the city most of its places
    belong to. No city, district, station or coordinate is invented here.
    """
    root = Path(plugin_dir)
    table = read_districts(root / DISTRICT_FILENAME)
    rows = read_places(root / SCENE_FILENAME, default_city=default_city)
    if not table and not rows:
        return {}

    # Which city each district belongs to, by majority of its places.
    votes: dict[str, dict[str, int]] = {}
    for row in rows:
        city = str(row.get("city") or "")
        district = str(row.get("district") or "")
        if not city or not district:
            continue
        votes.setdefault(district, {})
        votes[district][city] = votes[district].get(city, 0) + 1

    groups = components(table) if table else []
    district_city: dict[str, str] = {}
    for group in groups:
        tally: dict[str, int] = {}
        for district in group:
            for city, count in votes.get(district, {}).items():
                tally[city] = tally.get(city, 0) + count
        if not tally:
            continue
        city = max(tally.items(), key=lambda item: (item[1], item[0]))[0]
        for district in group:
            district_city[district] = city

    cities: dict[str, dict] = {}
    for district, city in district_city.items():
        entry = cities.setdefault(city, {
            "data": {city: {"districts": {}, "stations": []}},
            "activities": [], "province": "", "min_stay_days": 3,
            "city": city, "metadata": {"source": "legacy_files"},
        })
        entry["data"][city]["districts"][district] = dict(table[district])

    for row in rows:
        city = district_city.get(str(row.get("district") or "")) or str(row.get("city") or "")
        if not city or city not in cities:
            continue
        entry = cities[city]
        district = str(row.get("district") or "")
        entry["data"][city]["districts"].setdefault(district, {})
        entry["activities"].append({
            "name": row["name"], "district": district, "city": city,
            "kind": row["kind"], "place": row["place"],
        })
        if _is_station(row) and district not in entry["data"][city]["stations"]:
            entry["data"][city]["stations"].append(district)

    # Keep only complete records: districts without coordinates cannot drive
    # weather or movement, so such a city is not importable.
    cleaned: dict[str, dict] = {}
    for city, entry in cities.items():
        districts = {name: value for name, value in entry["data"][city]["districts"].items()
                     if isinstance(value, dict) and "lat" in value and "lon" in value}
        if not districts:
            continue
        for name, value in districts.items():
            value["neighbors"] = [n for n in value.get("neighbors", []) if n in districts]
        entry["data"][city]["districts"] = districts
        entry["data"][city]["stations"] = [d for d in entry["data"][city]["stations"] if d in districts]
        entry["activities"] = [a for a in entry["activities"] if a["district"] in districts]
        cleaned[city] = entry
    return cleaned
