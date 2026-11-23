"""Configure runtime-level coordination locks."""

from __future__ import annotations

from threading import RLock

from shared.keyed_locks import KeyedLockRegistry


class RuntimeCoordinationMixin:
    """Provide process-local locks for idempotent and scheduled operations."""

    def _configure_coordination(self) -> None:
        """Create locks that serialize idempotent and scheduled work."""
        self._idempotency_guard = RLock()
        # Bounded: idle locks are evicted (in-use locks never are).
        self._idempotency_locks = KeyedLockRegistry()
        self._execution_job_idempotency_locks = KeyedLockRegistry()
