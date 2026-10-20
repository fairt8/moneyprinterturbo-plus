"""JSON files for per-user settings and task ownership.

The bot process and the API process share these files through the storage
volume. Writes are atomic and, on Unix, guarded by a file lock.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


class JsonStore:
    def __init__(self, path: str):
        self.path = path
        self._thread_lock = threading.Lock()

    def _ensure_parent(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def update(self, mutator: Callable[[dict], None]) -> dict:
        with self._thread_lock:
            self._ensure_parent()
            lock_path = self.path + ".lock"
            with open(lock_path, "a", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    data = self._read()
                    mutator(data)
                    self._write(data)
                    return data
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict:
        with self._thread_lock:
            return self._read()

    def _read(self) -> dict:
        if not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _write(self, data: dict) -> None:
        self._ensure_parent()
        parent = os.path.dirname(self.path) or "."
        descriptor, temp_path = tempfile.mkstemp(prefix=".store-", dir=parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class SessionStore:
    def __init__(self, path: str):
        self._store = JsonStore(path)

    def get(self, user_id: int, defaults: dict) -> dict:
        key = str(user_id)
        current = self._store.read().get(key)
        if not isinstance(current, dict):
            current = {}
        merged = dict(defaults)
        merged.update(current)
        return merged

    def save(self, user_id: int, session: dict) -> None:
        key = str(user_id)

        def mutate(data: dict) -> None:
            data[key] = session

        self._store.update(mutate)


class JobStore:
    def __init__(self, path: str):
        self._store = JsonStore(path)

    def bind(self, task_id: str, owner: str) -> None:
        def mutate(data: dict) -> None:
            data[task_id] = {"owner": owner}

        self._store.update(mutate)

    def owner_of(self, task_id: str) -> str | None:
        record = self._store.read().get(task_id)
        if isinstance(record, dict):
            owner = record.get("owner")
            if isinstance(owner, str) and owner:
                return owner
        return None
