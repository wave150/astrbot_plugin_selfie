"""Single-character configuration, independent of AstrBot and image libraries.

Configuration mapping: each public data field below is read from ``character_``
plus its name (e.g. ``character_default_outfit`` -> ``default_outfit``).
``character_aliases`` must be a list of strings; other fields must be strings.
None and whitespace-only strings are treated as empty. Aliases are stripped,
deduplicated in order and stored as an immutable tuple.

With no nonempty character_* setting, caller-supplied legacy_defaults are used.
Legacy defaults accept unprefixed field names or the configuration keys (the
unprefixed key wins). No identity or bundled asset is hardcoded here. Any
nonempty character_* setting, including an unknown future key, selects custom
mode: missing fields stay empty and legacy_defaults are never consulted.

Custom references are local paths, relative to the caller's plugin directory
or absolute. Selection only checks file existence; no image is read. Legacy
style-specific asset and ref_image priority remains the caller's concern.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import os


_FIELDS = (
    "name", "aliases", "description", "hair", "default_outfit", "default_hat",
    "voice", "reference_body", "reference_head", "reference_single",
)


def _nonempty(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _text(value, key):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or None")
    if "\x00" in value:
        raise ValueError(f"{key} must not contain NUL characters")
    return value.strip()


@dataclass(frozen=True)
class CharacterProfile:
    """Immutable validated profile; use from_config at configuration boundaries."""

    mode: str = "legacy"
    name: str = ""
    aliases: tuple[str, ...] = ()
    description: str = ""
    hair: str = ""
    default_outfit: str = ""
    default_hat: str = ""
    voice: str = ""
    reference_body: str = ""
    reference_head: str = ""
    reference_single: str = ""

    @classmethod
    def from_config(cls, config=None, legacy_defaults=None):
        """Validate configuration without reading assets or importing main.

        Raises ValueError for malformed supported fields or non-mapping input.
        Missing custom name is allowed for asset-only/description-only profiles.
        Unknown character_* keys trigger custom isolation but are not copied.
        """
        if config is None:
            config = {}
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping or None")
        custom = any(
            isinstance(key, str) and key.startswith("character_") and _nonempty(value)
            for key, value in config.items()
        )
        # Validate supplied fields even when empty so malformed input cannot
        # silently reactivate legacy identity (e.g. character_name=[]).
        for name in _FIELDS:
            key = "character_" + name
            if key in config:
                cls._value(name, config[key])
        if custom:
            source = config
        else:
            source = {} if legacy_defaults is None else legacy_defaults
            if not isinstance(source, Mapping):
                raise ValueError("legacy_defaults must be a mapping or None")
        values = {}
        for name in _FIELDS:
            key = "character_" + name
            raw = source.get(key) if custom else source.get(name, source.get(key))
            values[name] = cls._value(name, raw)
        return cls(mode="custom" if custom else "legacy", **values)

    @staticmethod
    def _value(name, value):
        key = "character_" + name
        if name != "aliases":
            return _text(value, key)
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list of strings or None")
        result = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{key} must contain only strings")
            alias = _text(item, key)
            if alias and alias not in result:
                result.append(alias)
        return tuple(result)

    def storage_key(self):
        """Stable identity namespace; changing clothing/voice does not erase memories.

        Reference path changes represent a new identity until explicit migration.
        Original files remain untouched so returning to a profile restores its state.
        """
        if not self.is_custom:
            return "legacy"
        import hashlib
        import json
        identity = [self.name, self.description, self.reference_body, self.reference_head, self.reference_single]
        return "character_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]

    @property
    def is_custom(self):
        return self.mode == "custom"

    def reference_candidates(self, single=False):
        """Ordered, unique nonempty references from this profile only.

        A single-image site prefers the combined reference; otherwise prefer
        body. Head is a last resort if no body/combined reference is available.
        """
        paths = (self.reference_single, self.reference_body, self.reference_head) if single else (
            self.reference_body, self.reference_single, self.reference_head
        )
        return tuple(dict.fromkeys(path for path in paths if path))

    @staticmethod
    def _existing_path(path, base_dir):
        if not path:
            return ""
        resolved = path if os.path.isabs(path) else os.path.join(os.fspath(base_dir), path)
        return os.path.normpath(resolved) if os.path.isfile(resolved) else ""

    def pick_reference(self, base_dir, single=False):
        """Return the first existing local file, or '' (never a legacy asset)."""
        for path in self.reference_candidates(single=single):
            resolved = self._existing_path(path, base_dir)
            if resolved:
                return resolved
        return ""

    def pick_head_reference(self, base_dir):
        """Return the configured existing head file, or ''; style is caller-owned."""
        return self._existing_path(self.reference_head, base_dir)
