"""Single-executor guard for the purge of one organization.

A purge pass reads and rewrites the lifecycle record's checkpoint (``purge_step``,
``purged_rows``, ``clean_ticks``) across several independently committed steps, so
a database transaction cannot span it. The guard is a try-lock held for one pass:
a second process (or an overlapping tick) that cannot take it skips that
organization and tries again on its next tick. It never waits, so it never blocks
the worker loop, and it is per organization, so other organizations are not held.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol


class PurgeLock(Protocol):
    """Try-lock keyed by organization id."""

    def hold(self, org_id: str) -> AbstractContextManager[bool]:
        """Yield True while this caller is the only purger of ``org_id``.

        Yields False at once, without waiting, when another caller holds it. The
        lock is always released on exit, including when the body raises.
        """
        ...


class MemoryPurgeLock:
    """Process-local guard (in-memory store and tests)."""

    def __init__(self) -> None:
        """Start with no organization held."""
        self._guard = threading.Lock()
        self._held: set[str] = set()

    @contextmanager
    def hold(self, org_id: str) -> Iterator[bool]:
        """Try-lock ``org_id`` for the body of the ``with`` block."""
        with self._guard:
            acquired = org_id not in self._held
            if acquired:
                self._held.add(org_id)
        try:
            yield acquired
        finally:
            if acquired:
                with self._guard:
                    self._held.discard(org_id)
