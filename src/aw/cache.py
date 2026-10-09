"""Response caches: an in-memory TTL cache with an optional sqlite layer behind it.

Cached values are treated as read-only by convention (large vote lists are shared, not copied).
The disk layer is best-effort: any failure silently falls back to memory only.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

_MISS = object()


class TTLCache:
    """Small LRU + TTL cache."""

    def __init__(self, max_items: int = 512, clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max_items
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str, default: Any = None) -> Any:
        entry = self._data.get(key)
        if entry is None:
            return default
        expires, value = entry
        if expires <= self._clock():
            del self._data[key]
            return default
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (self._clock() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class DiskCache:
    """sqlite key/value store with wall-clock expiry. Keeps data warm across stdio restarts."""

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._conn: sqlite3.Connection | None = None
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(p), check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, value TEXT NOT NULL)"
            )
            self._conn.execute("DELETE FROM cache WHERE expires <= ?", (self._clock(),))
            self._conn.commit()
        except (sqlite3.Error, OSError):
            self._conn = None

    def get(self, key: str, default: Any = None) -> Any:
        if self._conn is None:
            return default
        try:
            row = self._conn.execute("SELECT expires, value FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None or row[0] <= self._clock():
                return default
            return json.loads(row[1])
        except (sqlite3.Error, ValueError):
            return default

    def set(self, key: str, value: Any, ttl: float) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (key, expires, value) VALUES (?, ?, ?)",
                (key, self._clock() + ttl, json.dumps(value, ensure_ascii=False)),
            )
            self._conn.commit()
        except (sqlite3.Error, TypeError, ValueError):
            pass

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class LayeredCache:
    """Memory first, then disk (promoting disk hits into memory)."""

    def __init__(self, memory: TTLCache, disk: DiskCache | None = None) -> None:
        self.memory = memory
        self.disk = disk

    def get(self, key: str) -> Any:
        value = self.memory.get(key, _MISS)
        if value is not _MISS:
            return value
        if self.disk is not None:
            value = self.disk.get(key, _MISS)
            if value is not _MISS:
                self.memory.set(key, value, ttl=600)
                return value
        return _MISS

    def set(self, key: str, value: Any, ttl: float) -> None:
        self.memory.set(key, value, ttl)
        if self.disk is not None:
            self.disk.set(key, value, ttl)


MISS = _MISS
