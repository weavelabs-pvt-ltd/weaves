"""Recurring workflow schedule contracts."""

from pydantic import AwareDatetime, Field, model_validator

from contracts.v1.base import (
    DisplayName,
    MutableWorkspaceScopedContract,
    OpaqueId,
    StrEnum,
)
from contracts.v1.executions import ErrorSummary


class WorkflowScheduleStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


class WorkflowSchedule(MutableWorkspaceScopedContract):
    """A recurring interval schedule bound to a workflow and its creator."""

    schedule_id: OpaqueId
    workflow_id: OpaqueId
    created_by_principal_id: OpaqueId
    name: DisplayName
    task_instructions: str = Field(min_length=1, max_length=2000)
    interval_seconds: int = Field(ge=60, le=31_536_000)
    status: WorkflowScheduleStatus
    next_run_at: AwareDatetime
    last_run_at: AwareDatetime | None = None
    last_error: ErrorSummary | None = None

    @model_validator(mode="after")
    def error_matches_status(self) -> "WorkflowSchedule":
        if (self.status is WorkflowScheduleStatus.ERROR) != (
            self.last_error is not None
        ):
            raise ValueError("error schedules require a last_error")
        return self


__all__ = ["WorkflowSchedule", "WorkflowScheduleStatus"]
