"""Compatibility methods for callers migrating to execution recovery use cases."""

from datetime import datetime

from application.runtime_constants import (
    STALE_EXECUTION_JOB_AFTER_SECONDS,
    STALE_RUN_AFTER_SECONDS,
)
from modules.executions.recovery_service import ExecutionRecoveryService
from shared.request_context import system_scoped


class RuntimeRecoveryCompatibilityMixin:
    """Preserve the historical runtime recovery method names."""

    recovery_service: ExecutionRecoveryService

    @system_scoped("recovery")
    def recover_stale_runs(
        self,
        *,
        stale_after_seconds: int = STALE_RUN_AFTER_SECONDS,
        now: datetime | None = None,
        sweep: bool = False,
    ) -> tuple[str, ...]:
        """Delegate stale run repair to the execution recovery service."""
        return self.recovery_service.recover_stale_runs(
            stale_after_seconds=stale_after_seconds, now=now, sweep=sweep
        )

    @system_scoped("recovery")
    def recover_stale_execution_jobs(
        self,
        *,
        stale_after_seconds: int = STALE_EXECUTION_JOB_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Delegate durable job repair to the execution recovery service."""
        return self.recovery_service.recover_stale_execution_jobs(
            stale_after_seconds=stale_after_seconds, now=now
        )

    def heartbeat_execution_job(
        self, job_id: str, *, worker_id: str, attempts: int
    ) -> bool:
        """Preserve the runtime heartbeat entrypoint for worker compatibility."""
        return self.recovery_service.heartbeat_execution_job(
            job_id, worker_id=worker_id, attempts=attempts
        )
