"""PostgreSQL session advisory lock guarding one organization's purge pass.

Purge steps commit independently, so a transaction-scoped lock cannot cover a
pass. A session lock on a dedicated pooled connection does: it is held while the
pass runs on other connections, and PostgreSQL drops it if the process dies, so a
crashed purger never strands an organization. Lock keys share the stable
``hashtextextended`` scheme used by the other advisory locks.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("product.worker")
_TRY = "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))"
_UNLOCK = "SELECT pg_advisory_unlock(hashtextextended(%s, 0))"


class PostgresPurgeLock:
    """Try-lock per organization, held on one borrowed connection per pass."""

    def __init__(self, store: Any) -> None:
        """Borrow connections from the store's pool (no tenant scope needed)."""
        self._store = store

    @contextmanager
    def hold(self, org_id: str) -> Iterator[bool]:
        """Take the lock without waiting; always release it on exit."""
        name = f"organization-purge:{org_id}"
        with self._store.pool.connection() as connection:
            row = connection.execute(_TRY, (name,)).fetchone()
            connection.commit()  # do not sit idle in a transaction for the pass
            acquired = bool(row and row[0])
            try:
                yield acquired
            finally:
                if acquired:
                    self._release(connection, name)

    @staticmethod
    def _release(connection: Any, name: str) -> None:
        """Unlock; a connection that cannot prove it unlocked is closed (drops it)."""
        try:
            if connection.closed:
                return
            connection.rollback()  # clear an aborted transaction first
            row = connection.execute(_UNLOCK, (name,)).fetchone()
            connection.commit()
            if not (row and row[0]):
                raise RuntimeError("purge lock was not held at release")
        except Exception:
            logger.exception("purge lock release failed; closing its connection")
            connection.close()
