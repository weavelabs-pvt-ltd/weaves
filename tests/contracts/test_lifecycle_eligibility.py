"""Suspended organizations and archived workspaces cannot start new work."""

from datetime import UTC, datetime, timedelta
from threading import Event
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import OrganizationStatus, WorkflowScheduleStatus, WorkspaceStatus
from shared.request_context import reset_request_principal, set_request_identity
from tests.contracts.test_mcp_oauth import _oauth_transport
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


def test_signed_webhook_is_refused_after_suspension_or_archive(monkeypatch):
    monkeypatch.setenv("WEAVES_API_TOKEN", "lifecycle-admin-token")
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    runtime.storage_mode = "postgres"
    headers = {"Authorization": "Bearer lifecycle-admin-token"}
    with TestClient(create_app(runtime)) as client:
        created = client.post(
            "/api/v0/workflows/local-assistant-workflow/triggers",
            headers=headers,
            json={"name": "Lifecycle", "task_instructions": "Run"},
        )
        assert created.status_code == 201, created.text
        url = "/api/v0/webhooks/" + created.json()["trigger"]["trigger_id"]
        secret = {"X-Workflow-Trigger-Secret": created.json()["secret"]}
        runtime.create_workspace(name="Second workspace")

        accepted = client.post(url, headers={**secret, "X-Event-ID": "a"}, json={})
        assert accepted.status_code == 202, accepted.text

        runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)
        archived = client.post(url, headers={**secret, "X-Event-ID": "b"}, json={})
        assert archived.status_code == 403
        runtime.update_workspace("local-workspace", status=WorkspaceStatus.ACTIVE)
        org = runtime.organizations.get("local-org")
        runtime.organizations.put(
            org.model_copy(update={"status": OrganizationStatus.SUSPENDED})
        )
        suspended = client.post(url, headers={**secret, "X-Event-ID": "c"}, json={})
        assert suspended.status_code == 403
    assert len(runtime.execution_jobs.list()) == 1


def test_schedule_skips_inactive_tenant_without_failing_and_resumes():
    runtime, _ = _runtime_with_second_workspace()
    schedule = runtime.create_workflow_schedule(
        "local-assistant-workflow",
        name="Lifecycle",
        task_instructions="Run",
        interval_seconds=60,
    )
    now = datetime.now(UTC)
    runtime.workflow_schedules.put(
        schedule.model_copy(update={"next_run_at": now - timedelta(minutes=1)})
    )
    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)

    assert runtime.dispatch_due_workflow_schedules(now=now) == ()

    stored = runtime.workflow_schedules.get(schedule.schedule_id)
    assert stored.status is WorkflowScheduleStatus.ACTIVE
    assert stored.last_error is None
    assert stored.next_run_at == now + timedelta(seconds=60)
    assert not runtime.execution_jobs.list()
    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ACTIVE)
    later = now + timedelta(seconds=61)
    assert len(runtime.dispatch_due_workflow_schedules(now=later)) == 1


def test_running_work_is_cancelled_at_the_next_safe_boundary():
    runtime, _ = _runtime_with_second_workspace()
    job, _ = runtime.enqueue_agent_run("Long running request")
    cancelled = Event()
    assert not runtime.execution_worker_service._execution_cancellation_requested(
        job.job_id, cancelled
    )

    runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)

    assert runtime.execution_worker_service._execution_cancellation_requested(
        job.job_id, cancelled
    )
    assert cancelled.is_set()


def test_oauth_continuation_is_refused_after_the_workspace_is_archived(monkeypatch):
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "tools.example.com,auth.example.com")
    monkeypatch.setenv("WEAVES_PUBLIC_API_BASE_URL", "https://api.weaves.test")
    http_client = httpx.Client(transport=httpx.MockTransport(_oauth_transport([])))
    runtime = LocalPlatformRuntime(http_client=http_client, postgres_store=None)
    runtime.mcp_plugin._client = http_client
    with TestClient(create_app(runtime)) as client:
        start = client.post(
            "/api/v0/oauth/mcp/start",
            json={
                "endpoint_url": "https://tools.example.com/mcp",
                "display_name": "Docs MCP",
                "connector_id": "custom-mcp",
            },
        )
        assert start.status_code == 200, start.text
        state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
        runtime.create_workspace(name="Second workspace")
        runtime.update_workspace("local-workspace", status=WorkspaceStatus.ARCHIVED)

        callback = client.get(
            "/api/v0/oauth/mcp/callback",
            params={"state": state, "code": "one-time-code"},
            follow_redirects=False,
        )

    assert callback.status_code == 303
    assert "connector_oauth=error" in callback.headers["location"]
    assert "connector_setup" not in callback.headers["location"]
