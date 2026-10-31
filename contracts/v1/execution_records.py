"""Execution records, action invocations, artifacts, and audit bundles."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from pydantic import (
    AwareDatetime,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from contracts.v1.agents import AgentVersion
from contracts.v1.base import (
    FrozenProductContract,
    MutableWorkspaceScopedContract,
    OpaqueId,
    ProductContract,
)
from contracts.v1.executions import (
    MAX_AGENT_RUN_OUTPUT_SUMMARY_BYTES,
    ApprovalStatus,
    ArtifactSensitivity,
    ArtifactStatus,
    AuditTargetScope,
    ErrorSummary,
    ExecutionJobStatus,
    InvocationStatus,
    RetentionClass,
    RunStatus,
    WorkflowRun,
    _validate_execution_timestamps,
    _validate_workspace_scope,
)
from contracts.v1.json_data import (
    freeze_json_value,
    reject_credential_fields,
    thaw_json_value,
)
from contracts.v1.plugins import Identifier, RiskLevel
from contracts.v1.workflows import WorkflowVersion


class ExecutionJob(MutableWorkspaceScopedContract):
    """Durable request envelope for work processed by a separate worker."""

    job_id: OpaqueId
    requested_by_principal_id: OpaqueId
    task: str = Field(min_length=1, max_length=12_000)
    agent_id: OpaqueId | None = None
    workflow_id: OpaqueId | None = None
    workflow_version_id: OpaqueId | None = None
    thread_id: OpaqueId | None = None
    trigger_id: OpaqueId | None = None
    trigger_event_id: OpaqueId | None = None
    schedule_id: OpaqueId | None = None
    schedule_occurrence_at: AwareDatetime | None = None
    evaluation_id: OpaqueId | None = None
    evaluation_suite_id: OpaqueId | None = None
    retry_of_job_id: OpaqueId | None = None
    idempotency_key: OpaqueId | None = None
    status: ExecutionJobStatus
    attempts: int = Field(default=0, ge=0, le=100)
    worker_id: OpaqueId | None = None
    run_id: OpaqueId | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: ErrorSummary | None = None

    @model_validator(mode="after")
    def fields_match_status(self) -> ExecutionJob:
        """Validate the target, retry linkage, and status-specific fields."""
        self._validate_job_target()
        self._validate_retry_reference()
        self._validate_job_status()
        return self

    def _validate_job_target(self) -> None:
        """Require a consistent execution target and trigger source."""
        if self.agent_id is not None and self.workflow_id is not None:
            raise ValueError("execution jobs cannot target both an agent and workflow")
        if self.workflow_version_id is not None and self.workflow_id is None:
            raise ValueError("workflow_version_id requires a workflow_id")
        if (self.trigger_id is None) != (self.trigger_event_id is None):
            raise ValueError("trigger jobs require both trigger and event IDs")
        if (self.schedule_id is None) != (self.schedule_occurrence_at is None):
            raise ValueError("scheduled jobs require both schedule and occurrence IDs")
        if (self.evaluation_id is None) != (self.evaluation_suite_id is None):
            raise ValueError("evaluation jobs require both evaluation IDs")
        if (
            sum(
                source is not None
                for source in (self.trigger_id, self.schedule_id, self.evaluation_id)
            )
            > 1
        ):
            raise ValueError("execution jobs cannot have multiple trigger sources")
        if (self.trigger_id is not None or self.schedule_id is not None) and (
            self.workflow_id is None
        ):
            raise ValueError("workflow triggers can only enqueue workflow jobs")
        if self.evaluation_id is not None and (
            self.agent_id is not None or self.workflow_id is not None
        ):
            raise ValueError("evaluation jobs cannot target an agent or workflow")

    def _validate_retry_reference(self) -> None:
        """Require retries to reference a distinct job and idempotency key."""
        if self.retry_of_job_id == self.job_id:
            raise ValueError("execution jobs cannot retry themselves")
        if self.retry_of_job_id is not None and self.idempotency_key is None:
            raise ValueError("retried jobs require an idempotency key")

    def _validate_job_status(self) -> None:
        """Validate state fields associated with each durable job status."""
        if self.status is ExecutionJobStatus.QUEUED:
            self._validate_queued_job()
        elif self.status in {
            ExecutionJobStatus.RUNNING,
            ExecutionJobStatus.CANCEL_REQUESTED,
        }:
            self._validate_active_job()
        elif self.status is ExecutionJobStatus.SUCCEEDED:
            self._validate_succeeded_job()
        elif self.status is ExecutionJobStatus.FAILED:
            self._validate_failed_job()
        elif self.status is ExecutionJobStatus.CANCELLED:
            self._validate_cancelled_job()

    def _validate_queued_job(self) -> None:
        """Reject execution fields on a job that has not started."""
        state = (
            self.worker_id,
            self.run_id,
            self.started_at,
            self.finished_at,
            self.error,
        )
        if self.attempts != 0 or any(value is not None for value in state):
            raise ValueError("queued jobs cannot have execution state")

    def _validate_active_job(self) -> None:
        """Require worker ownership and an open start interval for active jobs."""
        if (
            self.attempts < 1
            or self.worker_id is None
            or self.started_at is None
            or self.finished_at is not None
            or self.error is not None
        ):
            raise ValueError("active jobs require worker and start state")

    def _validate_succeeded_job(self) -> None:
        """Require one successful result and valid execution timestamps."""
        self._require_terminal_job_timestamps()
        if (self.run_id is None) == (self.evaluation_id is None):
            raise ValueError("succeeded jobs require one run or evaluation")
        if self.error is not None:
            raise ValueError("succeeded jobs cannot have an error")

    def _validate_failed_job(self) -> None:
        """Require timestamps and an error summary for failed jobs."""
        self._require_terminal_job_timestamps()
        if self.error is None:
            raise ValueError("failed jobs require an error summary")

    def _require_terminal_job_timestamps(self) -> None:
        """Require an attempt with both start and finish timestamps."""
        if self.attempts < 1 or self.started_at is None or self.finished_at is None:
            raise ValueError("terminal jobs require execution timestamps")

    def _validate_cancelled_job(self) -> None:
        """Require a finish timestamp and valid queued or active state."""
        if self.finished_at is None or self.error is not None:
            raise ValueError("cancelled jobs require a finish time and no error")
        if self.attempts == 0:
            self._validate_queued_cancellation()
            return
        if self.worker_id is None or self.started_at is None:
            raise ValueError("cancelled running jobs require worker and start state")

    def _validate_queued_cancellation(self) -> None:
        """Reject worker state on a job cancelled before execution."""
        state = (self.worker_id, self.run_id, self.started_at)
        if any(value is not None for value in state):
            raise ValueError("cancelled queued jobs cannot have execution state")


class AgentRun(MutableWorkspaceScopedContract):
    """One agent-level execution attempt and its metered model result."""

    agent_run_id: OpaqueId
    run_id: OpaqueId
    agent_id: OpaqueId
    agent_version_id: OpaqueId
    model_profile_id: OpaqueId
    resolved_provider_id: OpaqueId
    resolved_model_id: OpaqueId
    status: RunStatus
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: Decimal | None = Field(
        default=None, ge=Decimal("0"), max_digits=18, decimal_places=12
    )
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    error: ErrorSummary | None = None

    @field_validator("input_summary", "output_summary")
    @classmethod
    def summaries_are_bounded_json(cls, value: Any, info: Any) -> Any:
        """Reject credentials and oversized JSON summaries before storage."""
        if value is None:
            return None
        reject_credential_fields(value, info.field_name)
        frozen = freeze_json_value(value, info.field_name)
        max_bytes = (
            16_384
            if info.field_name == "input_summary"
            else MAX_AGENT_RUN_OUTPUT_SUMMARY_BYTES
        )
        encoded = json.dumps(
            thaw_json_value(frozen), allow_nan=False, ensure_ascii=False
        ).encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(
                f"{info.field_name} must not exceed {max_bytes} serialized bytes"
            )
        return frozen

    @field_serializer("input_summary", "output_summary")
    def serialize_summaries(self, value: Any) -> Any:
        """Convert immutable summary values back to JSON-compatible data."""
        return thaw_json_value(value) if value is not None else None

    @model_validator(mode="after")
    def timestamps_match_status(self) -> AgentRun:
        """Ensure execution timestamps and errors match the terminal state."""
        _validate_execution_timestamps(
            self.status, self.started_at, self.finished_at, self.error
        )
        return self


class ToolInvocation(MutableWorkspaceScopedContract):
    """One connector invocation with lifecycle, result, and approval state."""

    invocation_id: OpaqueId
    run_id: OpaqueId
    agent_run_id: OpaqueId | None = None
    plugin_installation_id: OpaqueId
    capability_id: Identifier
    status: InvocationStatus
    risk_level: RiskLevel
    approval_request_id: OpaqueId | None = None
    input_artifact_ids: tuple[OpaqueId, ...] = Field(
        default_factory=tuple, max_length=100
    )
    output_artifact_ids: tuple[OpaqueId, ...] = Field(
        default_factory=tuple, max_length=100
    )
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    error: ErrorSummary | None = None

    @field_validator("input_summary", "output_summary")
    @classmethod
    def summaries_are_bounded_json(cls, value: Any, info: Any) -> Any:
        """Reject credentials and oversized JSON summaries before storage."""
        if value is None:
            return None
        reject_credential_fields(value, info.field_name)
        frozen = freeze_json_value(value, info.field_name)
        if len(json.dumps(thaw_json_value(frozen), allow_nan=False)) > 16_384:
            raise ValueError(f"{info.field_name} must not exceed 16 KiB")
        return frozen

    @field_serializer("input_summary", "output_summary")
    def serialize_summaries(self, value: Any) -> Any:
        """Convert immutable summary values back to JSON-compatible data."""
        return thaw_json_value(value) if value is not None else None

    @model_validator(mode="after")
    def invocation_fields_match_outcome(self) -> ToolInvocation:
        """Validate invocation timing, approval, and error fields."""
        execution_terminal = self.status in {
            InvocationStatus.SUCCEEDED,
            InvocationStatus.FAILED,
            InvocationStatus.TIMED_OUT,
        }
        terminal = execution_terminal or self.status in {
            InvocationStatus.DENIED,
            InvocationStatus.SKIPPED,
        }
        if terminal and self.finished_at is None:
            raise ValueError("terminal invocations require finished_at")
        if not terminal and self.finished_at is not None:
            raise ValueError("non-terminal invocations cannot have finished_at")
        if self.status is InvocationStatus.EXECUTING and self.started_at is None:
            raise ValueError("executing invocations require started_at")
        if execution_terminal and (self.started_at is None or self.duration_ms is None):
            raise ValueError("completed executions require started_at and duration_ms")
        if (
            self.status in {InvocationStatus.FAILED, InvocationStatus.TIMED_OUT}
            and self.error is None
        ):
            raise ValueError(
                "failed and timed-out invocations require an error summary"
            )
        if (
            self.status not in {InvocationStatus.FAILED, InvocationStatus.TIMED_OUT}
            and self.error is not None
        ):
            raise ValueError(
                "only failed and timed-out invocations may include an error summary"
            )
        if (
            self.status
            in {
                InvocationStatus.AWAITING_APPROVAL,
                InvocationStatus.DENIED,
                InvocationStatus.APPROVED,
            }
            and self.approval_request_id is None
        ):
            raise ValueError(
                "approval-related invocation states require approval_request_id"
            )
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at cannot precede started_at")
        return self


class ArtifactProvenance(FrozenProductContract):
    """Source reference used to build an execution artifact."""

    source_type: Identifier
    source_ref: str = Field(min_length=1, max_length=256)
    recorded_at: AwareDatetime


class Artifact(MutableWorkspaceScopedContract):
    """Durable output and bounded provenance from an execution."""

    artifact_id: OpaqueId
    run_id: OpaqueId
    agent_run_id: OpaqueId | None = None
    artifact_type: Identifier
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=120_000)
    content_ref: OpaqueId
    content_type: Identifier
    checksum: str | None = Field(default=None, max_length=256)
    provenance: tuple[ArtifactProvenance, ...] = Field(min_length=1, max_length=50)
    sensitivity: ArtifactSensitivity
    retention_class: RetentionClass
    status: ArtifactStatus
    created_by_agent_run_id: OpaqueId | None = None
    created_at: AwareDatetime
    deleted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def deletion_timestamp_matches_status(self) -> Artifact:
        """Require deletion timestamps only for deleted artifacts."""
        if (self.status is ArtifactStatus.DELETED) != (self.deleted_at is not None):
            raise ValueError(
                "deleted artifacts require deleted_at; available artifacts must omit it"
            )
        return self


class ApprovalRequest(MutableWorkspaceScopedContract):
    """Human decision record for a proposed write capability."""

    approval_request_id: OpaqueId
    run_id: OpaqueId
    requested_by_agent_run_id: OpaqueId | None = None
    requested_by_principal_id: OpaqueId
    capability_id: Identifier
    action_summary: str = Field(min_length=1, max_length=1000)
    risk_level: RiskLevel
    status: ApprovalStatus
    requested_at: AwareDatetime
    resolved_by_principal_id: OpaqueId | None = None
    resolved_at: AwareDatetime | None = None
    decision_comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def decision_has_actor_and_time(self) -> ApprovalRequest:
        """Require actor and timestamp only for resolved approvals."""
        resolved = self.status is not ApprovalStatus.PENDING
        if resolved != (
            self.resolved_by_principal_id is not None and self.resolved_at is not None
        ):
            raise ValueError("resolved approvals require both decision actor and time")
        if not resolved and (
            self.resolved_by_principal_id is not None or self.resolved_at is not None
        ):
            raise ValueError("pending approvals cannot have decision actor or time")
        return self


class AuditEvent(FrozenProductContract):
    """Immutable audit event scoped to an organization or workspace."""

    audit_event_id: OpaqueId
    org_id: OpaqueId
    target_scope: AuditTargetScope
    workspace_id: OpaqueId | None = None
    actor_principal_id: OpaqueId
    action: Identifier
    target_type: Identifier
    target_id: OpaqueId
    summary: str = Field(default="", max_length=1000)
    request_id: OpaqueId
    correlation_id: OpaqueId | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime

    @field_validator("metadata")
    @classmethod
    def metadata_is_bounded_and_non_secret(cls, value: dict[str, Any]) -> Any:
        """Reject credentials and oversized audit metadata."""
        reject_credential_fields(value, "metadata")
        frozen = freeze_json_value(value, "metadata")
        if len(json.dumps(thaw_json_value(frozen), allow_nan=False)) > 8192:
            raise ValueError("metadata must not exceed 8 KiB")
        return frozen

    @field_serializer("metadata")
    def serialize_metadata(self, value: Any) -> Any:
        """Convert immutable audit metadata back to JSON-compatible data."""
        return thaw_json_value(value)

    @model_validator(mode="after")
    def workspace_target_is_scoped(self) -> AuditEvent:
        """Require a workspace ID only for workspace-scoped targets."""
        if (self.target_scope is AuditTargetScope.WORKSPACE) != (
            self.workspace_id is not None
        ):
            raise ValueError(
                "workspace targets require workspace_id; "
                "organization targets must omit it"
            )
        return self


class WorkflowRunBundle(ProductContract):
    """A workflow run pinned to the exact published workflow version."""

    run: WorkflowRun
    workflow_version: WorkflowVersion

    @model_validator(mode="after")
    def run_references_exact_version(self) -> WorkflowRunBundle:
        """Ensure a workflow run matches its exact definition version."""
        if self.run.workflow_version_id != self.workflow_version.workflow_version_id:
            raise ValueError("run must reference the supplied exact workflow version")
        if self.run.workflow_id != self.workflow_version.workflow_id:
            raise ValueError(
                "run and workflow version must reference the same workflow"
            )
        _validate_workspace_scope(self.run, self.workflow_version)
        return self


class AgentRunBundle(ProductContract):
    """An agent run pinned to the exact published agent version."""

    run: AgentRun
    agent_version: AgentVersion

    @model_validator(mode="after")
    def run_references_exact_agent_version(self) -> AgentRunBundle:
        """Ensure an agent run matches its exact agent version."""
        if self.run.agent_version_id != self.agent_version.agent_version_id:
            raise ValueError(
                "agent run must reference the supplied exact agent version"
            )
        if self.run.agent_id != self.agent_version.agent_id:
            raise ValueError("agent run and version must reference the same agent")
        _validate_workspace_scope(self.run, self.agent_version)
        return self
