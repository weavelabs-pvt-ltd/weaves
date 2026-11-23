"""A bounded registry of per-key re-entrant locks.

Idempotent requests serialize on a key inside one process. A plain dict of
locks grows with every key ever seen. This registry keeps at most
``max_entries`` idle locks (least recently used evicted first) and never evicts
a lock that is held or awaited, so mutual exclusion for a live key is exact.
Cross-process safety is, as before, the database's unique indexes.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from threading import RLock


class _Entry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = RLock()
        self.users = 0


class KeyedLockRegistry:
    """Hand out one lock per key with bounded idle memory."""

    def __init__(self, max_entries: int = 4096) -> None:
        """``max_entries`` bounds idle locks; in-use locks may exceed it."""
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._guard = RLock()
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()

    def __len__(self) -> int:
        """Number of locks currently retained."""
        with self._guard:
            return len(self._entries)

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        """Hold the lock for ``key`` for the duration of the block."""
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _Entry()
            else:
                self._entries.move_to_end(key)
            entry.users += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._guard:
                entry.users -= 1
                self._evict()

    def _evict(self) -> None:
        """Drop least recently used idle entries beyond the bound."""
        if len(self._entries) <= self._max_entries:
            return
        for key in list(self._entries):
            if len(self._entries) <= self._max_entries:
                break
            if self._entries[key].users == 0:
                del self._entries[key]
