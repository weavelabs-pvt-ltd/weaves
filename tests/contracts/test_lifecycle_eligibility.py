"""Suspended organizations and archived workspaces cannot start new work."""

import pytest

from contracts.v1 import OrganizationStatus, WorkspaceStatus
from shared.request_context import reset_request_principal, set_request_identity
from tests.support.runtime import LocalPlatformRuntime


def _runtime_with_second_workspace():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    second = runtime.create_workspace(name="Second workspace")
    return runtime, second


def test_archived_workspace_rejects_new_runs_and_jobs():
    runtime, second = _runtime_with_second_workspace()
    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)

    with pytest.raises(PermissionError, match="workspace is not active"):
        runtime.enqueue_agent_run("Queued after archive")
    with pytest.raises(PermissionError, match="workspace is not active"):
        runtime.run_agent("Run after archive")
    assert second.status is WorkspaceStatus.ACTIVE


def test_archived_workspace_can_still_be_restored_by_an_administrator():
    runtime, _ = _runtime_with_second_workspace()
    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)

    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ACTIVE)

    job, _ = runtime.enqueue_agent_run("Queued after restore")
    assert job.job_id


def test_suspended_organization_rejects_runs_and_admin_actions():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    org = runtime.organizations.get("local-org")
    runtime.organizations.put(
        org.model_copy(update={"status": OrganizationStatus.SUSPENDED})
    )

    with pytest.raises(PermissionError, match="organization is not active"):
        runtime.enqueue_agent_run("Queued while suspended")
    with pytest.raises(PermissionError, match="organization is not active"):
        runtime.list_managed_workspaces()


def test_worker_identity_for_an_archived_workspace_is_rejected():
    runtime, _ = _runtime_with_second_workspace()
    job, _ = runtime.enqueue_agent_run("Queued before archive")
    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)

    token = set_request_identity("local-developer", "local-org", "local-workspace")
    try:
        with pytest.raises(PermissionError, match="workspace is not active"):
            runtime.run_agent("Worker turn after archive")
    finally:
        reset_request_principal(token)
    assert runtime.execution_jobs.get(job.job_id).job_id == job.job_id
