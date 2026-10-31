"""Durable, persistence-agnostic execution and governance contracts."""

import re
from typing import TYPE_CHECKING, Any

from pydantic import (
    AwareDatetime,
    Field,
    field_validator,
    model_validator,
)

from contracts.v1.base import (
    FrozenProductContract,
    MutableWorkspaceScopedContract,
    OpaqueId,
    StrEnum,
)
from contracts.v1.plugins import Identifier, RiskLevel

if TYPE_CHECKING:
    from contracts.v1.execution_records import (
        AgentRun,
        AgentRunBundle,
        ApprovalRequest,
        Artifact,
        ArtifactProvenance,
        AuditEvent,
        ExecutionJob,
        ToolInvocation,
        WorkflowRunBundle,
    )

MAX_AGENT_RUN_OUTPUT_SUMMARY_BYTES = 2_097_152


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvocationStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    DENIED = "denied"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalPolicy(MutableWorkspaceScopedContract):
    """Workspace policy deciding which capability risks require human review."""

    approval_policy_id: OpaqueId
    name: str = Field(min_length=1, max_length=160)
    required_risk_levels: tuple[RiskLevel, ...] = Field(
        default=(RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL),
        max_length=4,
    )
    allow_self_approval: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def risk_levels_are_unique(self) -> "ApprovalPolicy":
        if len(self.required_risk_levels) != len(set(self.required_risk_levels)):
            raise ValueError("required risk levels must be unique")
        return self


class ArtifactStatus(StrEnum):
    AVAILABLE = "available"
    DELETED = "deleted"


class ArtifactSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RetentionClass(StrEnum):
    EPHEMERAL = "ephemeral"
    STANDARD = "standard"
    REGULATED = "regulated"
    LOCKED = "locked"


class AuditTargetScope(StrEnum):
    ORGANIZATION = "organization"
    WORKSPACE = "workspace"


class ErrorSummary(FrozenProductContract):
    # fmt: off
    """Bounded user-safe error details; """ \
    """stack traces and provider payloads do not belong here."""
    # fmt: on

    code: Identifier
    summary: str = Field(min_length=1, max_length=1000)
    retryable: bool = False

    @field_validator("summary")
    @classmethod
    def summary_is_sanitized(cls, value: str) -> str:
        if re.search(
            r"(?i)(bearer\s+\S+|(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*\S+)",
            value,
        ):
            raise ValueError("summary must not contain credential values")
        return value


class WorkflowRun(MutableWorkspaceScopedContract):
    run_id: OpaqueId
    workflow_id: OpaqueId
    workflow_version_id: OpaqueId
    requested_by_principal_id: OpaqueId
    trigger_type: Identifier
    trigger_ref: OpaqueId | None = None
    idempotency_key: OpaqueId | None = None
    status: RunStatus
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: ErrorSummary | None = None

    @model_validator(mode="after")
    def timestamps_match_status(self) -> "WorkflowRun":
        if self.status is RunStatus.QUEUED and (self.started_at or self.finished_at):
            raise ValueError("queued runs cannot have start or finish timestamps")
        if self.status is RunStatus.RUNNING and (
            self.started_at is None or self.finished_at is not None
        ):
            raise ValueError(
                "running runs require started_at and cannot have finished_at"
            )
        if self.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            if self.started_at is None or self.finished_at is None:
                raise ValueError("terminal runs require started_at and finished_at")
        if (self.status is RunStatus.FAILED) != (self.error is not None):
            raise ValueError(
                "failed runs require an error summary and other statuses must omit it"
            )
        return self


def _validate_workspace_scope(left: Any, right: Any) -> None:
    if left.org_id != right.org_id or left.workspace_id != right.workspace_id:
        raise ValueError(
            "execution record and definition version must share "
            "organization and workspace"
        )


def _validate_execution_timestamps(
    status: RunStatus,
    started_at: AwareDatetime | None,
    finished_at: AwareDatetime | None,
    error: ErrorSummary | None,
) -> None:
    if status is RunStatus.QUEUED and (
        started_at is not None or finished_at is not None
    ):
        raise ValueError("queued runs cannot have start or finish timestamps")
    if status is RunStatus.RUNNING and (started_at is None or finished_at is not None):
        raise ValueError("running runs require started_at and cannot have finished_at")
    if status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        if started_at is None or finished_at is None:
            raise ValueError("terminal runs require started_at and finished_at")
    if (status is RunStatus.FAILED) != (error is not None):
        raise ValueError(
            "failed runs require an error summary and other statuses must omit it"
        )


__all__ = [
    "AgentRun",
    "AgentRunBundle",
    "ApprovalRequest",
    "ApprovalStatus",
    "Artifact",
    "ArtifactProvenance",
    "ArtifactSensitivity",
    "ArtifactStatus",
    "AuditEvent",
    "AuditTargetScope",
    "ErrorSummary",
    "ExecutionJob",
    "InvocationStatus",
    "RetentionClass",
    "RunStatus",
    "ToolInvocation",
    "WorkflowRun",
    "WorkflowRunBundle",
]


# Domain records are split into a focused module while these imports preserve
# the established contracts.v1.executions import surface.
# Circular only through contract primitives already defined in this module.
from contracts.v1.execution_records import (  # noqa: E402
    AgentRun,
    AgentRunBundle,
    ApprovalRequest,
    Artifact,
    ArtifactProvenance,
    AuditEvent,
    ExecutionJob,
    ToolInvocation,
    WorkflowRunBundle,
)
