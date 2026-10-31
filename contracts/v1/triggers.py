"""Event-trigger contracts for published workflow execution."""

from typing import Literal

from pydantic import Field

from contracts.v1.base import (
    DisplayName,
    MutableWorkspaceScopedContract,
    OpaqueId,
    StrEnum,
)


class WorkflowTriggerStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class WorkflowTrigger(MutableWorkspaceScopedContract):
    """A secret-authenticated webhook bound to a published workflow."""

    trigger_id: OpaqueId
    workflow_id: OpaqueId
    created_by_principal_id: OpaqueId
    name: DisplayName
    trigger_type: Literal["webhook"] = "webhook"
    task_instructions: str = Field(min_length=1, max_length=2000)
    secret_digest: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    status: WorkflowTriggerStatus


__all__ = ["WorkflowTrigger", "WorkflowTriggerStatus"]
