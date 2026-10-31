from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from application import PluginGatewayError
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    MemoryRetentionClass,
    Permission,
    PluginInstallationStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
    RunStatus,
    WorkspaceStatus,
)
from tests.support.runtime import LocalPlatformRuntime


def test_recovery_fails_a_job_abandoned_by_worker():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Queue request")
    claimed = runtime.execution_jobs.claim_next_queued(
        "worker-crashed",
        datetime.now(UTC) - timedelta(hours=5),
        "local-org",
        "local-workspace",
    )
    assert claimed is not None
    current = datetime.now(UTC)

    recovered = runtime.recover_stale_execution_jobs(
        now=current, stale_after_seconds=60
    )

    assert recovered == (job.job_id,)
    stored = runtime.execution_jobs.get(job.job_id)
    assert stored.status is ExecutionJobStatus.FAILED
    assert stored.error is not None
    assert stored.error.code == "runtime.interrupted"


def test_recovery_finalizes_cancel_requested_job_after_worker_interruption():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Cancel and recover this job")
    claimed = runtime.execution_jobs.claim_next_queued(
        "worker-crashed",
        datetime.now(UTC) - timedelta(hours=5),
        "local-org",
        "local-workspace",
    )
    assert claimed is not None
    requested = runtime.cancel_execution_job(job.job_id)
    assert requested.status is ExecutionJobStatus.CANCEL_REQUESTED

    current = datetime.now(UTC)
    stale = ExecutionJob.model_validate(
        requested.model_copy(
            update={"updated_at": current - timedelta(minutes=5)}
        ).model_dump()
    )
    runtime.execution_jobs.put(stale)

    recovered = runtime.recover_stale_execution_jobs(
        now=current, stale_after_seconds=60
    )

    assert recovered == (job.job_id,)
    completed = runtime.execution_jobs.get(job.job_id)
    assert completed.status is ExecutionJobStatus.CANCELLED
    assert completed.run_id is None
    assert completed.error is None


def test_execution_job_lease_heartbeat_is_fenced_to_current_claim():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Lease renewal request")
    claimed = runtime.execution_jobs.claim_next_queued(
        "worker-current",
        datetime.now(UTC),
        "local-org",
        "local-workspace",
    )
    assert claimed is not None

    assert not runtime.heartbeat_execution_job(
        job.job_id, worker_id="worker-other", attempts=claimed.attempts
    )
    assert not runtime.heartbeat_execution_job(
        job.job_id, worker_id="worker-current", attempts=claimed.attempts + 1
    )
    assert runtime.heartbeat_execution_job(
        job.job_id, worker_id="worker-current", attempts=claimed.attempts
    )
    refreshed = runtime.execution_jobs.get(job.job_id)
    assert refreshed.updated_at >= claimed.updated_at

    recovered = runtime.recover_stale_execution_jobs(
        now=refreshed.updated_at + timedelta(seconds=61), stale_after_seconds=60
    )
    assert recovered == (job.job_id,)
    assert not runtime.execution_jobs.put_if_execution_job_owned(
        claimed, worker_id="worker-current", attempts=claimed.attempts
    )


def test_invitation_acceptance_rolls_back_all_records_when_audit_write_fails(
    monkeypatch,
):
    runtime = LocalPlatformRuntime()
    user, principal, _, invitation, token = runtime.create_user_invitation(
        email="transactional-invite@example.com",
        display_name="Transactional Invite",
        permissions=(Permission.AGENTS_RUN,),
    )
    create_audit = runtime.audit_events.create

    def fail_acceptance_audit(event):
        if event.action == "user.invitation_accepted":
            raise RuntimeError("simulated audit failure")
        return create_audit(event)

    monkeypatch.setattr(runtime.audit_events, "create", fail_acceptance_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        runtime.accept_user_invitation(token, "transactional-invite-password")

    assert (
        runtime.user_invitations.get(invitation.invitation_id).status.value == "pending"
    )
    assert runtime.users.get(user.id).status.value == "invited"
    assert runtime.principals.get(principal.id).status.value == "disabled"
    with pytest.raises(KeyError):
        runtime.user_passwords.get(user.id)
    assert not any(
        session.principal_id == principal.id for session in runtime.user_sessions.list()
    )


def test_concurrent_workspace_archives_preserve_one_active_workspace():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    workspaces = [runtime.workspaces.get("local-workspace")]
    workspaces.extend(
        runtime.create_workspace(name=f"Concurrent workspace {index}")
        for index in range(9)
    )

    def archive(workspace_id):
        try:
            runtime.update_workspace(workspace_id, status=WorkspaceStatus.ARCHIVED)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=len(workspaces)) as executor:
        results = tuple(executor.map(lambda item: archive(item.id), workspaces))

    assert sum(results) == len(workspaces) - 1
    active = tuple(
        item
        for item in runtime.workspaces.list_scoped("local-org")
        if item.status.value == "active"
    )
    assert len(active) == 1


def test_local_runtime_bootstraps_and_completes_a_scoped_mock_run():
    runtime = LocalPlatformRuntime()

    runtime.bootstrap()
    result = runtime.run_agent("Summarize our security basics")

    assert result.run.status is RunStatus.SUCCEEDED
    assert result.agent_run.agent_version_id == "local-assistant-v1"
    assert result.agent_run.resolved_provider_id == "local-mock-provider"
    assert result.tool_invocation.capability_id == "knowledge.search"
    assert result.artifact.provenance
    assert "least privilege" in result.artifact.summary
    assert result.audit_event.workspace_id == result.run.workspace_id
    assert runtime.runs.get(result.run.run_id) == result.run
    assert runtime.artifacts.get(result.artifact.artifact_id) == result.artifact


def test_local_runtime_rejects_empty_tasks_without_creating_a_run():
    runtime = LocalPlatformRuntime()

    with pytest.raises(ValueError, match="task must not be empty"):
        runtime.run_agent("  ")

    assert runtime.runs.list() == ()


def test_local_runtime_rejects_oversized_tasks_before_creating_a_run():
    runtime = LocalPlatformRuntime()

    with pytest.raises(ValueError, match="12000 characters"):
        runtime.run_agent("x" * 12_001)

    assert runtime.runs.list() == ()


def test_in_memory_repository_prevents_duplicate_creates():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()

    with pytest.raises(ValueError, match="already exists"):
        runtime.organizations.create(runtime.organizations.get("local-org"))


def test_in_memory_unit_of_work_rolls_back_all_repository_changes():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    workspace = runtime.workspaces.get("local-workspace")
    role = runtime.roles.get("local-developer-role")

    with (
        pytest.raises(RuntimeError, match="rollback transaction"),
        runtime._unit_of_work(),
    ):
        runtime.workspaces.put(
            workspace.model_copy(update={"name": "Uncommitted workspace"})
        )
        runtime.roles.delete(role.id)
        raise RuntimeError("rollback transaction")

    assert runtime.workspaces.get("local-workspace").name == "Default workspace"
    assert runtime.roles.get("local-developer-role") == role


def test_memory_changes_roll_back_when_the_audit_write_fails(monkeypatch):
    runtime = LocalPlatformRuntime()
    original_audit_create = runtime.audit_events.create
    prior_memory_ids = {
        item.memory_id for item in runtime.memory_items.list_scoped("local-org")
    }

    def fail_audit_create(_record):
        raise RuntimeError("simulated audit store failure")

    monkeypatch.setattr(runtime.audit_events, "create", fail_audit_create)
    with pytest.raises(RuntimeError, match="simulated audit store failure"):
        runtime.memory_service.create_memory_item(
            title="Atomic create", text="Keep the record audited."
        )
    assert {
        item.memory_id for item in runtime.memory_items.list_scoped("local-org")
    } == prior_memory_ids

    monkeypatch.setattr(runtime.audit_events, "create", original_audit_create)
    created = runtime.memory_service.create_memory_item(
        title="Atomic delete", text="Keep the deletion audited."
    )
    monkeypatch.setattr(runtime.audit_events, "create", fail_audit_create)
    with pytest.raises(RuntimeError, match="simulated audit store failure"):
        runtime.memory_service.delete_memory_item(created.memory_id)
    assert runtime.memory_items.get(created.memory_id) == created


def test_expired_memory_is_deleted_and_audited():
    runtime = LocalPlatformRuntime()
    expiry = datetime.now(UTC) + timedelta(seconds=1)
    item = runtime.memory_service.create_memory_item(
        title="Temporary context",
        text="This should be removed when it expires.",
        retention_class=MemoryRetentionClass.EPHEMERAL,
        expires_at=expiry,
    )

    assert runtime.memory_service.purge_expired_memory_items(now=expiry) == (
        item.memory_id,
    )
    with pytest.raises(KeyError):
        runtime.memory_items.get(item.memory_id)
    events = [
        event
        for event in runtime.audit_events.list()
        if event.action == "memory_item.expired" and event.target_id == item.memory_id
    ]
    assert len(events) == 1
    assert events[0].actor_principal_id == "system:memory-retention"
    assert runtime.memory_service.purge_expired_memory_items(now=expiry) == ()


def test_concurrent_memory_purge_audits_an_expired_item_once():
    runtime = LocalPlatformRuntime()
    expiry = datetime.now(UTC) + timedelta(seconds=1)
    item = runtime.memory_service.create_memory_item(
        title="Concurrent expiry",
        text="Only one maintenance worker should remove this.",
        retention_class=MemoryRetentionClass.EPHEMERAL,
        expires_at=expiry,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: runtime.memory_service.purge_expired_memory_items(now=expiry),
                range(2),
            )
        )

    assert [memory_id for result in results for memory_id in result] == [item.memory_id]
    assert (
        sum(
            event.action == "memory_item.expired" and event.target_id == item.memory_id
            for event in runtime.audit_events.list()
        )
        == 1
    )


def test_repository_does_not_expose_mutable_internal_records():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    first_read = runtime.workspaces.get("local-workspace")
    first_read.name = "Mutated outside repository"

    assert runtime.workspaces.get("local-workspace").name == "Default workspace"


def test_repository_reads_enforce_organization_and_workspace_scope():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()

    with pytest.raises(KeyError, match="organization scope"):
        runtime.installations.get_scoped(
            "local-knowledge-installation", "another-org", "local-workspace"
        )
    assert runtime.installations.list_scoped("local-org", "local-workspace") == (
        runtime.installations.get("local-knowledge-installation"),
    )
    assert runtime.installations.list_scoped("another-org") == ()


def test_local_runtime_requires_run_permission():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    now = datetime.now(UTC)
    runtime.principals.create(
        Principal(
            id="unassigned-principal",
            org_id="local-org",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            status=PrincipalStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )

    with pytest.raises(PermissionError, match=r"lacks agents\.run permission"):
        runtime.run_agent(
            "Read handbook", requested_by_principal_id="unassigned-principal"
        )

    assert runtime.runs.list() == ()


def test_plugin_denial_fails_run_and_records_invocation_and_audit():
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="No context agent",
        instructions="Answer without using external context.",
        allowed_capability_ids=("knowledge.search",),
    )
    runtime.integration_admin_service.update_plugin_installation(
        "local-knowledge-installation",
        status=PluginInstallationStatus.DISABLED,
    )

    with pytest.raises(PluginGatewayError, match="disabled"):
        runtime.run_agent("Read the handbook", agent_id=agent.id)

    run = runtime.runs.list()[0]
    invocation = runtime.tool_invocations.list()[0]
    assert run.status is RunStatus.FAILED
    assert invocation.status.value == "failed"
    assert invocation.error is not None
    assert runtime.audit_events.list()[-1].action == "agent.run.failed"
