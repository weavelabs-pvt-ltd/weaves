"""Construct the dependencies for the worker process."""

from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_wiring import open_process_runtime
from apps.worker.services import WorkerServices


def build_platform_runtime() -> LocalPlatformRuntime:
    """Open worker-owned pools and compose the platform runtime."""
    return open_process_runtime(process="worker")


def build_worker_services() -> WorkerServices:
    """Build explicit worker dependencies and retain runtime-owned cleanup."""
    runtime = build_platform_runtime()
    return WorkerServices(
        storage_mode=runtime.storage_mode,
        bootstrap=runtime.bootstrap,
        close=runtime.close,
        execution=runtime.execution_worker_service,
        execution_recovery=runtime.recovery_service,
        action_recovery=runtime.action_recovery_service,
        memory=runtime.memory_service,
        workflow_automation=runtime.workflow_automation_service,
    )
