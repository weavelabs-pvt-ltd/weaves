"""Durable agent task, transcript, and turn contracts."""

from pydantic import AwareDatetime, Field, model_validator

from contracts.v1.base import (
    DisplayName,
    ImmutableWorkspaceVersionContract,
    MutableWorkspaceScopedContract,
    OpaqueId,
    StrEnum,
)
from contracts.v1.executions import ErrorSummary


class TaskStatus(StrEnum):
    """Lifecycle state chosen by the user for a task."""

    OPEN = "open"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TaskMessageRole(StrEnum):
    """Human-facing speaker roles supported by task conversations."""

    USER = "user"
    ASSISTANT = "assistant"


class TaskTurnStatus(StrEnum):
    """Durable processing state for one user message and agent response."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(MutableWorkspaceScopedContract):
    """User-owned task conversation pinned to one agent version."""

    task_id: OpaqueId
    title: DisplayName
    description: str = Field(default="", max_length=4000)
    created_by_principal_id: OpaqueId
    requested_by_principal_id: OpaqueId
    agent_id: OpaqueId
    agent_version_id: OpaqueId
    thread_id: OpaqueId
    creation_message_id: OpaqueId
    status: TaskStatus = TaskStatus.OPEN
    active_turn_id: OpaqueId | None = None
    active_turn_status: TaskTurnStatus | None = None
    latest_turn_id: OpaqueId | None = None
    latest_turn_status: TaskTurnStatus | None = None
    latest_response_summary: str | None = Field(default=None, max_length=2000)
    idempotency_key: OpaqueId | None = None
    completed_at: AwareDatetime | None = None
    archived_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def timestamps_match_status(self) -> "Task":
        """Keep completion and archive timestamps consistent with lifecycle."""
        if (self.status is TaskStatus.COMPLETED) != (self.completed_at is not None):
            raise ValueError("completed tasks require completed_at only")
        if (self.status is TaskStatus.ARCHIVED) != (self.archived_at is not None):
            raise ValueError("archived tasks require archived_at only")
        if self.status is not TaskStatus.OPEN and self.active_turn_id is not None:
            raise ValueError("completed or archived tasks cannot have an active turn")
        if (self.active_turn_id is None) != (self.active_turn_status is None):
            raise ValueError("active turn ID and status must be set together")
        return self


class TaskMessage(ImmutableWorkspaceVersionContract):
    """Immutable transcript entry linked to its task turn."""

    message_id: OpaqueId
    task_id: OpaqueId
    turn_id: OpaqueId
    role: TaskMessageRole
    content: str = Field(min_length=1, max_length=32_000)
    created_by_principal_id: OpaqueId | None = None

    @model_validator(mode="after")
    def creator_matches_role(self) -> "TaskMessage":
        """Attribute human messages while keeping agent replies unowned."""
        if (self.role is TaskMessageRole.USER) != (
            self.created_by_principal_id is not None
        ):
            raise ValueError("only user task messages require a creator")
        return self


class TaskTurn(MutableWorkspaceScopedContract):
    """Internal durable dispatch and outcome for one task conversation turn."""

    turn_id: OpaqueId
    task_id: OpaqueId
    user_message_id: OpaqueId
    assistant_message_id: OpaqueId | None = None
    requested_by_principal_id: OpaqueId
    idempotency_key: OpaqueId | None = None
    run_idempotency_key: OpaqueId | None = None
    status: TaskTurnStatus
    attempts: int = Field(default=0, ge=0, le=100)
    worker_id: OpaqueId | None = None
    agent_run_id: OpaqueId | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: ErrorSummary | None = None

    @model_validator(mode="after")
    def fields_match_status(self) -> "TaskTurn":
        """Validate lease, result, error, and timestamp invariants."""
        validators = {
            TaskTurnStatus.QUEUED: self._validate_queued,
            TaskTurnStatus.RUNNING: self._validate_running,
            TaskTurnStatus.SUCCEEDED: self._validate_succeeded,
            TaskTurnStatus.FAILED: self._validate_failed,
            TaskTurnStatus.CANCELLED: self._validate_cancelled,
        }
        validators[self.status]()
        return self

    def _validate_queued(self) -> None:
        """Ensure queued turns have no active execution lease or result."""
        active_fields = (
            self.worker_id,
            self.agent_run_id,
            self.assistant_message_id,
            self.started_at,
            self.finished_at,
            self.error,
        )
        if any(value is not None for value in active_fields):
            raise ValueError("queued task turns cannot contain execution state")

    def _validate_running(self) -> None:
        """Require the active attempt's worker lease and start timestamp."""
        if (
            self.attempts < 1
            or self.worker_id is None
            or self.started_at is None
            or self.finished_at is not None
            or self.assistant_message_id is not None
            or self.error is not None
        ):
            raise ValueError("running task turns require an active worker lease")

    def _validate_succeeded(self) -> None:
        """Require a completed attempt and its persisted assistant message."""
        self._validate_terminal()
        if self.assistant_message_id is None or self.error is not None:
            raise ValueError("successful task turns require an assistant message")

    def _validate_failed(self) -> None:
        """Require a completed attempt and safe failure summary."""
        self._validate_terminal()
        if self.error is None or self.assistant_message_id is not None:
            raise ValueError("failed task turns require a safe error summary")

    def _validate_cancelled(self) -> None:
        """Require a finish timestamp and no failure record for cancellations."""
        if self.finished_at is None or self.error is not None:
            raise ValueError("cancelled task turns require finished_at only")

    def _validate_terminal(self) -> None:
        """Require a started attempt and completion timestamp for terminal work."""
        if self.attempts < 1 or self.started_at is None or self.finished_at is None:
            raise ValueError("terminal task turns require start and finish timestamps")


__all__ = [
    "Task",
    "TaskMessage",
    "TaskMessageRole",
    "TaskStatus",
    "TaskTurn",
    "TaskTurnStatus",
]
