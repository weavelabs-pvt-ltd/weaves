"""Ensure deprecated run-lab and execution-job APIs are absent."""

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


def test_run_lab_and_history_routes_are_not_registered():
    app = create_app(LocalPlatformRuntime())
    with TestClient(app):
        paths = app.openapi()["paths"]

    removed_paths = {
        "/api/v0/runs",
        "/api/v0/runs/{run_id}",
        "/api/v0/runs/{run_id}/artifacts",
        "/api/v0/runs/{run_id}/workflow-progress",
        "/api/v0/runs/{run_id}/actions",
        "/api/v0/agents/{agent_id}/runs",
        "/api/v0/threads/{thread_id}/runs",
        "/api/v0/workflows/{workflow_id}/runs",
        "/api/v0/execution-jobs",
        "/api/v0/execution-jobs/{job_id}",
        "/api/v0/execution-jobs/{job_id}/workflow-progress",
        "/api/v0/execution-jobs/{job_id}/cancel",
        "/api/v0/execution-jobs/{job_id}/retry",
        "/api/v0/artifacts",
    }

    assert removed_paths.isdisjoint(paths)
    assert "/api/v0/agents" in paths
    assert "/api/v0/workflows" in paths
    assert "/api/v0/audit-events" in paths
