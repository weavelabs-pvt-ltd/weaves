from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
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


def test_member_provisioning_rolls_back_all_records_when_audit_write_fails(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    before_users = runtime.users.list()
    before_principals = runtime.principals.list()
    before_bindings = runtime.role_bindings.list()
    role = next(r for r in runtime.roles.list() if r.system_role_key == "user")

    def fail_audit(event):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(runtime.audit_events, "create", fail_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        runtime.create_workspace_user(
            email="transactional@example.com",
            display_name="Transactional",
            role_id=role.id,
        )
    assert runtime.users.list() == before_users
    assert runtime.principals.list() == before_principals
    assert runtime.role_bindings.list() == before_bindings


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
    assert result.tool_invocation is None
    assert result.artifact.provenance == ()
    assert "Local assistant response" in result.artifact.summary
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








def test_repository_does_not_expose_mutable_internal_records():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    first_read = runtime.workspaces.get("local-workspace")
    first_read.name = "Mutated outside repository"

    assert runtime.workspaces.get("local-workspace").name == "Default workspace"


def test_repository_reads_enforce_organization_and_workspace_scope():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()

    assert runtime.installations.list_scoped("local-org", "local-workspace") == ()
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
