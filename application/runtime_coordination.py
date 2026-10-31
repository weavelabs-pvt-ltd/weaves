"""Configure runtime-level coordination locks."""

from __future__ import annotations

from threading import RLock


class RuntimeCoordinationMixin:
    """Provide process-local locks for idempotent and scheduled operations."""

    def _configure_coordination(self) -> None:
        """Create locks that serialize idempotent and scheduled work."""
        self._idempotency_guard = RLock()
        self._idempotency_locks: dict[tuple[str, str], RLock] = {}
        self._execution_job_idempotency_locks: dict[tuple[str, str], RLock] = {}
