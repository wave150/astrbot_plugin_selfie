"""Schema-free JSON persistence; locking is per instance, not cross-process."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import threading


class JsonStateStore:
    """Preserve legacy top-level merge semantics and publish complete files only.

    Share one instance between threads. Separate instances/processes are not
    coordinated; the last successful replace wins. Caller-owned state must not
    be mutated concurrently while save takes its snapshot.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self, defaults: dict) -> dict:
        if not isinstance(defaults, dict):
            raise ValueError("State defaults must be a JSON object")
        with self._lock:
            result = copy.deepcopy(defaults)
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    data = json.load(stream)
            except FileNotFoundError:
                return result
            if not isinstance(data, dict):
                raise ValueError("Stored state must be a JSON object")
            result.update(data)
            return result

    def save(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object")
        with self._lock:
            # Serialize before touching disk; invalid values cannot damage old state.
            payload = json.dumps(copy.deepcopy(state), ensure_ascii=False,
                                 indent=1, allow_nan=False)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix="." + self.path.name + ".", suffix=".tmp", delete=False,
                ) as stream:
                    temporary = stream.name
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
