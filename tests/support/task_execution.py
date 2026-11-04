"""Test helpers for exercising an agent through the public task lifecycle."""

from fastapi.testclient import TestClient

from apps.platform_runtime import LocalPlatformRuntime


def run_task(
    client: TestClient,
    runtime: LocalPlatformRuntime,
    message: str,
    *,
    agent_id: str = "local-assistant",
    title: str = "Contract test task",
    headers: dict[str, str] | None = None,
) -> tuple[dict, object, object, list]:
    """Create a task, process its queued turn, and return persisted evidence."""
    response = client.post(
        "/api/v0/tasks",
        headers=headers,
        json={
            "agent_id": agent_id,
            "title": title,
            "initial_message": message,
        },
    )
    assert response.status_code == 202, response.text
    task = response.json()
    turn = runtime.task_worker_service.process_next_turn("contract-test-worker")
    assert turn is not None, "created task did not produce a queued turn"
    agent_run = (
        runtime.agent_runs.get(turn.agent_run_id)
        if turn.agent_run_id is not None
        else None
    )
    artifacts = [
        artifact
        for artifact in runtime.artifacts.list_scoped(
            task["org_id"], task["workspace_id"]
        )
        if artifact.agent_run_id == turn.agent_run_id
    ]
    return task, turn, agent_run, artifacts
