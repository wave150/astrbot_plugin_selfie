"""Lossless numbered material files and explicitly confirmed, in-memory drafts.

No constructor/read performs migration or calls an LLM. Writers cooperate through
an exclusive sibling lock and compare SHA-256 revisions immediately before replace.
Uncooperative external editors cannot participate in the lock; their observed
changes are rejected, but filesystem compare-and-swap is not generally available.
Draft tokens are process-local; after restart an unknown token is rejected.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid


KINDS = ("costume", "closeup", "interaction", "meal")
FILENAMES = {"costume": "costumes.txt", "closeup": "closeup_prompts.txt",
             "interaction": "interaction.txt", "meal": "meals.txt"}
# 随包默认素材的「种子版本」：升级时把新增的默认条目补进数据目录文件，
# 用户改过/删掉的条目不会被还原（版本号相同就不再补）。
SEED_VERSIONS = {"meal": 2}
SEED_MARKER = ".materials_seed.json"
_NUMBER = re.compile(r"^\s*([0-9]+)\s*[：:.、)）]\s*")


class MaterialError(ValueError):
    """Invalid material or draft."""


class MaterialConflict(MaterialError):
    """File revision changed or another writer holds the lock."""


class MaterialDuplicate(MaterialError):
    """Content or costume name/alias already exists."""


def _kind(kind):
    if kind not in KINDS:
        raise MaterialError("kind must be costume, closeup, interaction or meal")
    return kind


def _entry(kind, ident, text):
    entry = {"id": ident, "text": text}
    if not text.strip():
        raise MaterialError("empty material")
    if kind == "costume":
        parts = [part.strip() for part in text.split("|")]
        # Legacy parser also accepts name | description.
        if len(parts) not in (2, 3) or not parts[0] or not parts[-1]:
            raise MaterialError("costume requires name | aliases | description")
        entry.update(name=parts[0], description=parts[-1], aliases=[
            a.strip() for a in re.split(r"[,，、/]+", parts[1] if len(parts) == 3 else "")
            if a.strip()])
    elif kind == "meal":
        parts = [part.strip() for part in text.split("|")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise MaterialError("meal requires name | description")
        entry.update(name=parts[0], description=parts[1])
    elif kind == "interaction":
        match = re.match(r"^(摸摸|捏捏)[：:｜| ]\s*", text)
        entry["action"] = match[1] if match else ""
    return entry


def _rows(kind, text):
    _kind(kind)
    rows, explicit = [], set()
    for index, raw in enumerate(text.splitlines(keepends=True)):
        line = raw.rstrip("\r\n")
        if index == 0:
            line = line.removeprefix("\ufeff")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _NUMBER.match(line)
        ident = int(match[1]) if match else None
        if ident is not None:
            if ident < 1 or ident in explicit:
                raise MaterialError("material IDs must be unique positive integers")
            explicit.add(ident)
        rows.append((index, ident, line[match.end():] if match else line))
    next_id = 1
    result = []
    for index, ident, body in rows:
        if ident is None:
            while next_id in explicit:
                next_id += 1
            ident = next_id
            explicit.add(ident)
            next_id += 1
        result.append((index, _entry(kind, ident, body)))
    return result


def parse_materials(kind: str, text: str) -> list[dict]:
    """Parse without writes; explicit IDs survive sorting/deletion.

    Unnumbered legacy rows receive free IDs in order. Persist these with migrate
    before editing/reordering legacy files if stable identity is required.
    text excludes only the number prefix, not interaction action labels.
    """
    return [entry for _, entry in _rows(kind, text)]


def _bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _revision(data):
    return hashlib.sha256(data).hexdigest() if data is not None else "missing"


def _decode(data):
    return data.decode("utf-8") if data is not None else ""


def ensure_files(root, default_root, kinds=KINDS):
    """首次运行时把随包默认素材复制到数据目录；已存在的不覆盖。

    运行时读写必须落在数据目录（升级/迁移不会丢），插件目录只当只读默认值。
    返回实际复制出来的文件名列表。
    """
    root, default_root = Path(root), Path(default_root)
    copied = []
    for kind in kinds:
        target = root / FILENAMES[kind]
        if target.exists():
            continue
        source = default_root / FILENAMES[kind]
        if not source.exists() and kind == "interaction":
            source = default_root / "小汉堡互动.txt"
        if not source.exists():
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
            data = source.read_bytes()
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)
            copied.append(FILENAMES[kind])
        except OSError:
            continue
    return copied


def merge_seed(root, default_root, kind, version):
    """把随包默认条目按名称补进数据目录文件（一次性，按 ``version`` 记账）。

    用途：默认菜品从 9 道扩到 53 道时，老用户数据目录里只有旧的 9 道。这里把
    默认文件里「名字还没出现过」的条目追加进去，已有条目（包括用户改过描述、
    改过名字的）一律不动。同一个版本号只补一次，用户之后删掉的默认条目不会被
    再次还原。返回本次追加的条目名字列表。
    """
    kind = _kind(kind)
    root, default_root = Path(root), Path(default_root)
    marker_path = root / SEED_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = {}
    if not isinstance(marker, dict):
        marker = {}
    if int(marker.get(kind) or 0) >= int(version):
        return []
    target = root / FILENAMES[kind]
    source = default_root / FILENAMES[kind]
    if not target.exists() or not source.exists():
        return []
    existing = parse_materials(kind, target.read_text(encoding="utf-8"))
    defaults = parse_materials(kind, source.read_text(encoding="utf-8"))
    names = {str(entry.get("name") or "").strip().casefold() for entry in existing}
    additions = [entry for entry in defaults
                 if str(entry.get("name") or "").strip().casefold() not in names]
    if additions:
        data = target.read_bytes()
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        if data and not data.endswith((b"\n", b"\r")):
            data += newline
        next_id = max((entry["id"] for entry in existing), default=0) + 1
        for entry in additions:
            data += f"{next_id}：{entry['text']}".encode("utf-8") + newline
            next_id += 1
        temporary = target.with_name(target.name + ".tmp")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
    marker[kind] = int(version)
    temporary = marker_path.with_name(marker_path.name + ".tmp")
    try:
        root.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, marker_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return [str(entry.get("name") or "") for entry in additions]


def _numbered(kind, data):
    text = _decode(data)
    rows = _rows(kind, text)
    lines = text.splitlines(keepends=True)
    for index, entry in rows:
        line = lines[index]
        bom = "\ufeff" if index == 0 and line.startswith("\ufeff") else ""
        body = line[len(bom):]
        if not _NUMBER.match(body):
            lines[index] = f"{bom}{entry['id']}：{body}"
    return "".join(lines).encode("utf-8")


class MaterialStore:
    """paths optionally maps kinds to configured absolute or root-relative paths."""

    def __init__(self, root, paths=None, *, backup_dir=None, fallback_root=None):
        self.root = Path(root)
        self.fallback_root = Path(fallback_root) if fallback_root else None
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.paths = {}
        for kind in KINDS:
            path = Path((paths or {}).get(kind) or FILENAMES[kind])
            self.paths[kind] = path if path.is_absolute() else self.root / path
        self._drafts = {}
        self._mutex = threading.RLock()

    def _source(self, kind):
        path = self.paths[_kind(kind)]
        legacy_names = ("小汉堡互动.txt",) if kind == "interaction" else ()
        for name in legacy_names:
            if not path.exists() and (self.root / name).exists():
                return self.root / name
        if not path.exists() and self.fallback_root is not None:
            # 插件目录里的文件只是随包默认值；运行时读写始终落在数据目录。
            candidate = self.fallback_root / path.name
            if candidate.exists():
                return candidate
            for name in legacy_names:
                if (self.fallback_root / name).exists():
                    return self.fallback_root / name
        return path

    def snapshot(self, kind):
        source = self._source(kind)
        data = _bytes(source)
        return {"kind": kind, "revision": _revision(data),
                "entries": parse_materials(kind, _decode(data))}

    def list(self, kind):
        return self.snapshot(kind)["entries"]

    @contextmanager
    def _locked(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_name(path.name + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise MaterialConflict("material file is locked") from exc
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink()

    @staticmethod
    def _atomic(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _write(self, source, original, target, target_original, data):
        def check():
            if _bytes(source) != original or _bytes(target) != target_original:
                raise MaterialConflict("material file changed; create a new draft")
        check()
        # Backup is complete and durable before replacing any existing data.
        if original is not None:
            if self.backup_dir is not None:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                backup = self.backup_dir / (source.name + "." + uuid.uuid4().hex + ".bak")
            else:
                backup = source.with_name(source.name + "." + uuid.uuid4().hex + ".bak")
            self._atomic(backup, original)
        check()
        self._atomic(target, data)

    def migrate(self, kind):
        """Explicit opt-in: number legacy rows, copy legacy interaction to new name.

        Keeps the old interaction file; an existing new file is authoritative.
        Returns a snapshot. Caller, not construction/listing, controls migration.
        """
        target = self.paths[_kind(kind)]
        with self._mutex, self._locked(target):
            source = self._source(kind)
            original = _bytes(source)
            if original is None:
                return self.snapshot(kind)
            data = _numbered(kind, original)
            if source != target or data != original:
                self._write(source, original, target, _bytes(target), data)
            return self.snapshot(kind)

    @staticmethod
    def _validate(kind, text):
        _kind(kind)
        if not isinstance(text, str) or not text.strip() or len(text.splitlines()) != 1:
            raise MaterialError("draft must contain exactly one nonempty line")
        if any(ord(c) < 32 or c in "\u2028\u2029" for c in text):
            raise MaterialError("draft contains control characters")
        if text.lstrip().startswith("#") or _NUMBER.match(text):
            raise MaterialError("draft must not contain a comment or numeric ID prefix")
        if kind == "costume" and len(text.split("|")) != 3:
            raise MaterialError("costume draft requires name | aliases | description")
        if kind == "meal":
            parts = [part.strip() for part in text.split("|")]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise MaterialError("meal draft requires name | description")
            if len(parts[1]) < 20:
                raise MaterialError("meal description is too short; describe the dish and tableware")
        return _entry(kind, 0, text)

    def create_draft(self, kind, text):
        self._validate(kind, text)
        with self._mutex:
            source = self._source(kind)
            original = _bytes(source)
            # Reject malformed files before presenting a draft.
            parse_materials(kind, _decode(original))
            token = uuid.uuid4().hex
            draft = {"token": token, "kind": kind, "text": text,
                     "revision": _revision(original)}
            self._drafts[token] = {"draft": draft, "source": source,
                                   "original": original, "target_original": _bytes(self.paths[kind])}
            return deepcopy(draft)

    def edit_draft(self, token, text):
        with self._mutex:
            state = self._get(token)
            if "result" in state:
                raise MaterialError("draft already confirmed")
            self._validate(state["draft"]["kind"], text)
            state["draft"]["text"] = text
            return deepcopy(state["draft"])

    def _get(self, token):
        if token not in self._drafts:
            raise MaterialError("unknown or expired draft token")
        return self._drafts[token]

    def discard_draft(self, token):
        with self._mutex:
            self._drafts.pop(token, None)

    def confirm_draft(self, token):
        with self._mutex:
            state = self._get(token)
            if "result" in state:
                return deepcopy(state["result"])
            draft = state["draft"]
            kind, text = draft["kind"], draft["text"]
            candidate = self._validate(kind, text)
            target = self.paths[kind]
            with self._locked(target):
                if (self._source(kind) != state["source"] or
                        _bytes(state["source"]) != state["original"] or
                        _bytes(target) != state["target_original"]):
                    raise MaterialConflict("material file changed; create a new draft")
                entries = parse_materials(kind, _decode(state["original"]))
                for entry in entries:
                    if entry["text"].strip() == text.strip():
                        raise MaterialDuplicate("material text already exists")
                    if kind == "costume":
                        old = {s.casefold() for s in [entry["name"], *entry["aliases"]]}
                        new = {s.casefold() for s in [candidate["name"], *candidate["aliases"]]}
                        if old & new:
                            raise MaterialDuplicate("costume name or alias already exists")
                candidate["id"] = max((e["id"] for e in entries), default=0) + 1
                data = _numbered(kind, state["original"])
                newline = b"\r\n" if b"\r\n" in data else b"\n"
                if data and not data.endswith((b"\n", b"\r")):
                    data += newline
                data += f"{candidate['id']}：{text}".encode("utf-8") + newline
                self._write(state["source"], state["original"], target,
                            state["target_original"], data)
                state["result"] = candidate
                return deepcopy(candidate)

    def _locate(self, kind, ident, data):
        """Return (row index, entry) for an existing material id, or raise."""
        entries = _rows(kind, _decode(data))
        for index, entry in entries:
            if entry["id"] == ident:
                return index, entry
        raise MaterialError("material id not found")

    def update(self, kind, ident, text, revision=None):
        """Replace one existing row's text in place; the id and order stay stable."""
        kind = _kind(kind)
        if type(ident) is not int or ident <= 0:
            raise MaterialError("material id must be a positive integer")
        candidate = self._validate(kind, text)
        target = self.paths[kind]
        with self._mutex, self._locked(target):
            source = self._source(kind)
            original = _bytes(source)
            if original is None:
                raise MaterialError("material file is missing")
            if revision is not None and revision != _revision(original):
                raise MaterialConflict("material file changed; refresh and retry")
            index, entry = self._locate(kind, ident, original)
            if entry["text"].strip() == text.strip():
                return {"kind": kind, "entry": dict(candidate, id=ident),
                        "revision": _revision(original), "entries": parse_materials(kind, _decode(original))}
            for other_index, other in _rows(kind, _decode(original)):
                if other_index == index:
                    continue
                if other["text"].strip() == text.strip():
                    raise MaterialDuplicate("material text already exists")
                if kind == "costume":
                    old = {s.casefold() for s in [other["name"], *other["aliases"]]}
                    new = {s.casefold() for s in [candidate["name"], *candidate["aliases"]]}
                    if old & new:
                        raise MaterialDuplicate("costume name or alias already exists")
            lines = _decode(original).splitlines(keepends=True)
            line = lines[index]
            bom = "\ufeff" if index == 0 and line.startswith("\ufeff") else ""
            ending = "\r\n" if line.endswith("\r\n") else ("\r" if line.endswith("\r") else "\n")
            lines[index] = f"{bom}{ident}：{text}{ending}"
            data = "".join(lines).encode("utf-8")
            self._write(source, original, target, _bytes(target), data)
            updated = dict(candidate, id=ident)
            return {"kind": kind, "entry": updated, "revision": _revision(data),
                    "entries": parse_materials(kind, data.decode("utf-8"))}

    def delete(self, kind, ident, revision=None):
        """Remove one row; other rows keep their ids so references stay valid."""
        kind = _kind(kind)
        if type(ident) is not int or ident <= 0:
            raise MaterialError("material id must be a positive integer")
        target = self.paths[kind]
        with self._mutex, self._locked(target):
            source = self._source(kind)
            original = _bytes(source)
            if original is None:
                raise MaterialError("material file is missing")
            if revision is not None and revision != _revision(original):
                raise MaterialConflict("material file changed; refresh and retry")
            index, entry = self._locate(kind, ident, original)
            if len(_rows(kind, _decode(original))) <= 1:
                raise MaterialError("material library must keep at least one entry")
            lines = _decode(original).splitlines(keepends=True)
            del lines[index]
            data = "".join(lines).encode("utf-8")
            self._write(source, original, target, _bytes(target), data)
            return {"kind": kind, "entry": dict(entry, id=ident), "revision": _revision(data),
                    "entries": parse_materials(kind, data.decode("utf-8"))}
