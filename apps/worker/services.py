"""Hold explicit module services and lifecycle actions for the worker app."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from modules.executions.queue_health import QueueHealthService
from modules.executions.recovery_service import ExecutionRecoveryService
from modules.executions.worker_service import ExecutionWorkerService
from modules.tasks.worker_service import TaskWorkerService
from modules.workflows.automation_service import WorkflowAutomationService
from modules.workflows.studio_service import WorkflowStudioService


@dataclass(frozen=True)
class WorkerServices:
    """Expose only the capabilities and process hooks used by the worker."""

    storage_mode: str
    bootstrap: Callable[[], None]
    close: Callable[[], None]
    execution: ExecutionWorkerService
    execution_recovery: ExecutionRecoveryService
    workflow_automation: WorkflowAutomationService
    tasks: TaskWorkerService
    studio: WorkflowStudioService | None = None
    purge_accounts: Callable[[], int] | None = None
    purge_organizations: Callable[[], int] | None = None
    purge_waitlist: Callable[[], int] | None = None
    queue_health: QueueHealthService | None = None
    reconcile_usage: Callable[[], int] | None = None
    purge_usage: Callable[[], int] | None = None
    # Slack lifecycle: finish half-retired tokens and expire the event inbox.
    slack_lifecycle: Callable[[], int] | None = None
    # Slack bot (S4a): refresh connector health snapshots across organizations.
    refresh_slack_health: Callable[[], int] | None = None
    # Slack bot (S4): turn mentions into hidden Q&A turns, post their replies and
    # expire the hidden conversations.
    dispatch_slack: Callable[[], int] | None = None
    reply_slack: Callable[[], int] | None = None
    purge_slack: Callable[[], int] | None = None
    # Slack bot (S4c): thumbs reactions to ratings, and their 400 day retention.
    feedback_slack: Callable[[], int] | None = None
    purge_slack_feedback: Callable[[], int] | None = None
    # True while an operator has paused claiming (``platform_control.py``).
    worker_paused: Callable[[], bool] | None = None
