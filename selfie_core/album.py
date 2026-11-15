"""Bounded local album for workbench-generated images; no network or external paths."""
import io
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .storage import JsonStateStore

_SAFE_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_ALLOWED_SOURCES = ("test", "try_costume", "selfie")


def _atomic_write(path, data):
    """Write bytes beside the target, then replace; never leave a partial file."""
    temporary = path.with_name("." + path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with open(temporary, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _thumbnail(image_bytes):
    """Return a compact JPEG thumbnail, or None when Pillow is unavailable/invalid."""
    try:
        from PIL import Image
    except Exception:  # Pillow is optional for this module.
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            converted = image.convert("RGB")
            converted.thumbnail((480, 480))
            output = io.BytesIO()
            converted.save(output, format="JPEG", quality=82, optimize=True)
            return output.getvalue()
    except Exception:
        return None


class AlbumStore:
    """Metadata index plus PNG files; every mutation is validated and bounded."""

    version = 1

    def __init__(self, directory, *, max_items=300):
        self.directory = Path(directory)
        self.max_items = max(1, int(max_items))
        self._lock = threading.RLock()
        self._store = JsonStateStore(self.directory / "index.json")

    def _load(self):
        document = self._store.load({"version": self.version, "items": []})
        if not isinstance(document.get("items"), list):
            raise ValueError("album index is invalid")
        items = [item for item in document["items"] if isinstance(item, dict) and _SAFE_ID.match(str(item.get("id")))]
        return {"version": self.version, "items": items}

    @staticmethod
    def _valid_meta(meta):
        meta = meta if isinstance(meta, dict) else {}
        allowed = ("city", "district", "place", "costume_name", "costume_id",
                   "closeup_id", "event_name", "event_id", "room", "scene_index")
        result = {}
        for key in allowed:
            value = meta.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and value not in ("", None):
                result[key] = value
        return result

    def save(self, image_bytes, *, source="test", meta=None, created_at=None):
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise ValueError("image must be nonempty bytes")
        if len(image_bytes) > _MAX_IMAGE_BYTES:
            raise ValueError("image is too large")
        if source not in _ALLOWED_SOURCES:
            raise ValueError("unsupported album source")
        created = float(created_at if created_at is not None else time.time())
        if not (0 <= created <= 4 * 10 ** 10):
            raise ValueError("created_at is out of range")
        item_id = uuid.uuid4().hex
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            document = self._load()
            image = self.directory / (item_id + ".png")
            thumbnail = self.directory / (item_id + ".jpg")
            _atomic_write(image, bytes(image_bytes))
            thumb_bytes = _thumbnail(image_bytes)
            thumb_flag = False
            if thumb_bytes:
                try:
                    _atomic_write(thumbnail, thumb_bytes)
                    thumb_flag = True
                except OSError:
                    thumb_flag = False
            item = {"id": item_id, "created_at": created, "source": source,
                    "size": len(image_bytes), "thumb": thumb_flag, **self._valid_meta(meta)}
            document["items"].insert(0, item)
            removed = []
            while len(document["items"]) > self.max_items:
                removed.append(document["items"].pop())
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                self._store.save(document)
            except Exception:
                # Metadata write failed: keep old index, drop the orphan files.
                for path in (image, thumbnail):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                raise
            for old in removed:
                self._delete_files(old)
            return dict(item)

    def _delete_files(self, item):
        item_id = str(item.get("id") or "")
        if not _SAFE_ID.match(item_id):
            return
        for suffix in (".png", ".jpg"):
            try:
                (self.directory / (item_id + suffix)).unlink()
            except OSError:
                pass

    def _visible(self, document=None):
        """Items whose full image still exists, unsorted."""
        document = self._load() if document is None else document
        return [item for item in document["items"]
                if (self.directory / (str(item["id"]) + ".png")).is_file()]

    def count(self):
        """Total visible photos, so the UI can page without loading everything."""
        with self._lock:
            document = self._load()
        return len(self._visible(document))

    def list(self, limit=60, offset=0):
        """Newest first; entries whose full image is missing are skipped."""
        try:
            limit = min(200, max(1, int(limit)))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            raise ValueError("limit/offset must be integers")
        with self._lock:
            document = self._load()
        items = self._visible(document)
        items.sort(key=lambda item: (-float(item.get("created_at", 0)), str(item.get("id"))))
        selected = items[offset:offset + limit]
        return [dict(item, thumb=bool(item.get("thumb") and
                                     (self.directory / (str(item["id"]) + ".jpg")).is_file()))
                for item in selected]

    def _path(self, item_id, suffix):
        text = str(item_id or "")
        if not _SAFE_ID.match(text):
            return None
        return self.directory / (text + suffix)

    def read(self, item_id):
        path = self._path(item_id, ".png")
        if path is None or not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def thumb(self, item_id):
        path = self._path(item_id, ".jpg")
        if path is None:
            return None
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None
