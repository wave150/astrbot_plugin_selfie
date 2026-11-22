"""用户上传的动作线稿库，无内置素材，编号递增且删除后不复用"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading
import time

from .ref_library import RefLibraryError, MAX_UPLOAD_BYTES, _decode_image, _text

_LOCKS = {}
_GUARD = threading.Lock()
_ID = re.compile(r"^[1-9][0-9]{0,15}$")


class PoseSketchLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.document_path = self.root / "library.json"
        with _GUARD:
            self._lock = _LOCKS.setdefault(str(self.root.resolve()), threading.RLock())

    @staticmethod
    def _id(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return ""
        text = str(value)
        return text if _ID.fullmatch(text) else ""

    def _load(self):
        if not self.document_path.exists():
            # Recover the high-water mark from files after an interrupted first upload
            highest = max((int(p.stem) for p in self.root.glob("*.png")
                           if self._id(p.stem)), default=0)
            return {"version": 1, "next_id": highest + 1, "images": {}}
        try:
            doc = json.loads(self.document_path.read_text(encoding="utf-8"))
            if (not isinstance(doc, dict) or doc.get("version") != 1
                    or not isinstance(doc.get("images"), dict)
                    or type(doc.get("next_id")) is not int or doc["next_id"] < 1):
                raise ValueError()
        except (ValueError, OSError):
            raise RefLibraryError("线稿素材索引读取失败，已有数据未改动") from None
        highest = max((int(key) for key in doc["images"] if self._id(key)), default=0)
        doc["next_id"] = max(doc["next_id"], highest + 1)
        return doc

    def _save(self, doc):
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic(self.document_path, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))

    @staticmethod
    def _atomic(path, data):
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _path(self, ident, *, thumb=False):
        key = self._id(ident)
        if not key:
            return ""
        path = self.root / (key + ("_thumb.jpg" if thumb else ".png"))
        # Never follow a file symlink outside the dedicated library
        return str(path) if path.is_file() and path.resolve().parent == self.root.resolve() else ""

    def image_path(self, ident):
        with self._lock:
            key = self._id(ident)
            return self._path(key) if key in self._load()["images"] else ""

    def thumb_path(self, ident):
        with self._lock:
            key = self._id(ident)
            return self._path(key, thumb=True) if key in self._load()["images"] else ""

    def rows(self):
        with self._lock:
            return [dict(deepcopy(meta), id=int(key))
                    for key, meta in sorted(self._load()["images"].items(), key=lambda x: int(x[0]) if self._id(x[0]) else 0)
                    if self._id(key) and isinstance(meta, dict) and self._path(key)]

    def get(self, ident):
        key = self._id(ident)
        return next((row for row in self.rows() if str(row["id"]) == key), None)

    def snapshot(self):
        with self._lock:
            return {"images": self.rows(), "next_id": self._load()["next_id"]}

    def read(self, ident, *, thumb=False):
        path = self.thumb_path(ident) if thumb else self.image_path(ident)
        if not path:
            raise RefLibraryError("线稿特写不存在，请刷新后重试")
        try:
            return Path(path).read_bytes()
        except OSError:
            raise RefLibraryError("线稿图片读取失败，请重新上传") from None

    def save_image(self, data, *, label=""):
        label = _text(label, 40, "线稿名称")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise RefLibraryError("线稿图片数据无效，请重新选择图片")
        if len(data) > MAX_UPLOAD_BYTES:
            raise RefLibraryError("线稿图片过大，请压缩到 12MB 以内")
        full, thumb, width, height = _decode_image(bytes(data))
        with self._lock:
            doc = self._load()
            ident = doc["next_id"]
            doc["next_id"] = ident + 1
            # Persist reservation before files so failed writes never reuse a number
            self._save(doc)
            self._atomic(self.root / f"{ident}.png", full)
            self._atomic(self.root / f"{ident}_thumb.jpg", thumb)
            meta = {"label": label or f"线稿特写 {ident}", "width": width,
                    "height": height, "created_at": time.time()}
            doc["images"][str(ident)] = meta
            self._save(doc)
            return dict(meta, id=ident)

    def rename_image(self, ident, label):
        label = _text(label, 40, "线稿名称")
        with self._lock:
            key = self._id(ident)
            doc = self._load()
            if not key or key not in doc["images"]:
                raise RefLibraryError("线稿特写不存在，请刷新后重试")
            doc["images"][key]["label"] = label or f"线稿特写 {key}"
            self._save(doc)
            return dict(doc["images"][key], id=int(key))

    def delete_image(self, ident):
        with self._lock:
            key = self._id(ident)
            doc = self._load()
            if not key or key not in doc["images"]:
                raise RefLibraryError("线稿特写不存在，请刷新后重试")
            paths = [self._path(key), self._path(key, thumb=True)]
            del doc["images"][key]
            self._save(doc)
            for path in paths:
                if path:
                    Path(path).unlink(missing_ok=True)
            return {"deleted": int(key)}
