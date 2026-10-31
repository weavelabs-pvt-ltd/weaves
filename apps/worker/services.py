"""Hold explicit module services and lifecycle actions for the worker app."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from modules.executions.recovery_service import ExecutionRecoveryService
from modules.executions.worker_service import ExecutionWorkerService
from modules.governance.action_recovery_service import ActionRecoveryService
from modules.memory.runtime import MemoryService
from modules.workflows.automation_service import WorkflowAutomationService


@dataclass(frozen=True)
class WorkerServices:
    """Expose only the capabilities and process hooks used by the worker."""

    storage_mode: str
    bootstrap: Callable[[], None]
    close: Callable[[], None]
    execution: ExecutionWorkerService
    execution_recovery: ExecutionRecoveryService
    action_recovery: ActionRecoveryService
    memory: MemoryService
    workflow_automation: WorkflowAutomationService
