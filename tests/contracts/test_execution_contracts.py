from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.v1 import (
    AgentRun,
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    ArtifactProvenance,
    ArtifactSensitivity,
    ArtifactStatus,
    AuditEvent,
    AuditTargetScope,
    ErrorSummary,
    ExecutionJob,
    ExecutionJobStatus,
    InvocationStatus,
    RetentionClass,
    RiskLevel,
    RunStatus,
    ToolInvocation,
    WorkflowRun,
)

NOW = datetime.now(UTC)


def workflow_run(**overrides):
    values = {
        "run_id": "run-1",
        "org_id": "org-1",
        "workspace_id": "workspace-1",
        "workflow_id": "workflow-1",
        "workflow_version_id": "workflow-version-3",
        "requested_by_principal_id": "principal-1",
        "trigger_type": "api.request",
        "status": RunStatus.SUCCEEDED,
        "started_at": NOW,
        "finished_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return WorkflowRun(**(values | overrides))


def test_runs_require_exact_version_and_terminal_timestamps():
    with pytest.raises(ValidationError):
        workflow_run(workflow_version_id=None)
    with pytest.raises(ValidationError, match="terminal runs require"):
        workflow_run(finished_at=None)
    with pytest.raises(ValidationError, match="exact workflow version"):
        from contracts.v1 import WorkflowRunBundle

        WorkflowRunBundle(
            run=workflow_run(),
            workflow_version={
                "workflow_version_id": "workflow-version-2",
                "workflow_id": "workflow-1",
                "org_id": "org-1",
                "workspace_id": "workspace-1",
                "version": 3,
                "handler_key": "support.triage",
                "agent_version_ids": ("agent-v1",),
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "created_at": NOW,
            },
        )


def test_failed_run_requires_bounded_sanitized_error():
    with pytest.raises(ValidationError, match="error summary"):
        workflow_run(status=RunStatus.FAILED, error=None)
    with pytest.raises(ValidationError, match="credential values"):
        ErrorSummary(code="provider.failure", summary="api_key=secret-value")
    with pytest.raises(ValidationError):
        ErrorSummary(code="provider.failure", summary="x" * 1001)
    assert (
        workflow_run(
            status=RunStatus.FAILED,
            error=ErrorSummary(
                code="provider.failure", summary="Provider request failed"
            ),
        ).error
        is not None
    )


def test_execution_job_state_requires_claim_and_terminal_fields():
    base = {
        "job_id": "job-1",
        "org_id": "org-1",
        "workspace_id": "workspace-1",
        "requested_by_principal_id": "principal-1",
        "task": "Summarize the release checklist",
        "agent_id": "agent-1",
        "status": ExecutionJobStatus.QUEUED,
        "created_at": NOW,
        "updated_at": NOW,
    }
    queued = ExecutionJob(**base)
    assert queued.attempts == 0
    workflow_queued = ExecutionJob(
        **(base | {"agent_id": None, "workflow_id": "workflow-1"})
    )
    assert workflow_queued.workflow_id == "workflow-1"
    pinned_workflow_queued = ExecutionJob(
        **(
            base
            | {
                "agent_id": None,
                "workflow_id": "workflow-1",
                "workflow_version_id": "workflow-version-3",
            }
        )
    )
    assert pinned_workflow_queued.workflow_version_id == "workflow-version-3"
    with pytest.raises(ValidationError, match="requires a workflow_id"):
        ExecutionJob(**(base | {"workflow_version_id": "workflow-version-3"}))
    with pytest.raises(ValidationError, match="both an agent and workflow"):
        ExecutionJob(**(base | {"workflow_id": "workflow-1"}))
    with pytest.raises(ValidationError, match="active jobs require"):
        ExecutionJob(**(base | {"status": ExecutionJobStatus.RUNNING, "attempts": 1}))
    running = ExecutionJob(
        **(
            base
            | {
                "status": ExecutionJobStatus.RUNNING,
                "attempts": 1,
                "worker_id": "worker-a",
                "started_at": NOW,
            }
        )
    )
    assert running.worker_id == "worker-a"
    with pytest.raises(ValidationError, match="failed jobs require"):
        ExecutionJob(
            **(
                base
                | {
                    "status": ExecutionJobStatus.FAILED,
                    "attempts": 1,
                    "started_at": NOW,
                    "finished_at": NOW,
                }
            )
        )


def test_agent_run_captures_resolved_provider_and_model_ids():
    run = AgentRun(
        agent_run_id="agent-run-1",
        run_id="run-1",
        org_id="org-1",
        workspace_id="workspace-1",
        agent_id="agent-1",
        agent_version_id="agent-v4",
        model_profile_id="profile-1",
        resolved_provider_id="provider-1",
        resolved_model_id="model-2026-01",
        status=RunStatus.RUNNING,
        started_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    assert run.resolved_provider_id == "provider-1"
    assert run.resolved_model_id == "model-2026-01"


@pytest.mark.parametrize(
    "status",
    [
        InvocationStatus.DENIED,
        InvocationStatus.APPROVED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
    ],
)
def test_invocation_records_distinguish_governance_and_execution_states(status):
    values = {
        "invocation_id": "invocation-1",
        "run_id": "run-1",
        "org_id": "org-1",
        "workspace_id": "workspace-1",
        "plugin_installation_id": "install-1",
        "capability_id": "ticket.create",
        "status": status,
        "risk_level": RiskLevel.HIGH,
        "created_at": NOW,
        "updated_at": NOW,
    }
    if status in {InvocationStatus.DENIED, InvocationStatus.APPROVED}:
        values["approval_request_id"] = "approval-1"
    if status in {
        InvocationStatus.DENIED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
    }:
        values["finished_at"] = NOW
    if status in {InvocationStatus.SUCCEEDED, InvocationStatus.FAILED}:
        values.update(started_at=NOW, duration_ms=10)
    if status is InvocationStatus.FAILED:
        values["error"] = ErrorSummary(code="tool.failure", summary="Tool failed")
    record = ToolInvocation(**values)
    assert record.status is status


def test_artifact_requires_provenance_and_retention_class():
    values = {
        "artifact_id": "artifact-1",
        "org_id": "org-1",
        "workspace_id": "workspace-1",
        "run_id": "run-1",
        "artifact_type": "report.summary",
        "title": "Summary",
        "content_ref": "blob-ref-1",
        "content_type": "application.json",
        "provenance": (
            ArtifactProvenance(
                source_type="agent.run", source_ref="agent-run-1", recorded_at=NOW
            ),
        ),
        "sensitivity": ArtifactSensitivity.INTERNAL,
        "retention_class": RetentionClass.STANDARD,
        "status": ArtifactStatus.AVAILABLE,
        "created_at": NOW,
        "updated_at": NOW,
    }
    assert Artifact(**values).retention_class is RetentionClass.STANDARD
    with pytest.raises(ValidationError):
        Artifact(**(values | {"provenance": ()}))
    with pytest.raises(ValidationError):
        Artifact(**(values | {"retention_class": "unknown"}))


def test_approval_decisions_require_actor_and_time():
    values = {
        "approval_request_id": "approval-1",
        "org_id": "org-1",
        "workspace_id": "workspace-1",
        "run_id": "run-1",
        "requested_by_principal_id": "principal-1",
        "capability_id": "ticket.create",
        "action_summary": "Create ticket",
        "risk_level": RiskLevel.HIGH,
        "status": ApprovalStatus.APPROVED,
        "requested_at": NOW,
        "resolved_by_principal_id": "principal-2",
        "resolved_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    assert ApprovalRequest(**values).resolved_by_principal_id == "principal-2"
    with pytest.raises(ValidationError, match="decision actor and time"):
        ApprovalRequest(**(values | {"resolved_at": None}))


def test_audit_events_are_immutable_and_workspace_targets_are_scoped():
    event = AuditEvent(
        audit_event_id="audit-1",
        org_id="org-1",
        target_scope=AuditTargetScope.WORKSPACE,
        workspace_id="workspace-1",
        actor_principal_id="principal-1",
        action="workflow.run.start",
        target_type="workflow_run",
        target_id="run-1",
        request_id="request-1",
        metadata={"source": "api"},
        created_at=NOW,
    )
    with pytest.raises(ValidationError):
        event.action = "workflow.run.delete"
    with pytest.raises(ValidationError, match="workspace targets require"):
        AuditEvent(
            audit_event_id="audit-2",
            org_id="org-1",
            target_scope=AuditTargetScope.WORKSPACE,
            actor_principal_id="principal-1",
            action="workflow.run.start",
            target_type="workflow_run",
            target_id="run-1",
            request_id="request-1",
            created_at=NOW,
        )


def test_execution_contracts_do_not_expose_raw_credential_fields():
    for model in (
        WorkflowRun,
        AgentRun,
        ToolInvocation,
        Artifact,
        ApprovalRequest,
        AuditEvent,
    ):
        assert not {
            "credential",
            "password",
            "secret",
            "api_key",
            "access_token",
        }.intersection(model.model_fields)
