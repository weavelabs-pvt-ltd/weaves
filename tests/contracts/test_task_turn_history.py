"""A task's turn history: every turn's outcome, in order, without internals.

The task page needs each turn's status and user-safe error, not only the latest
one, so an earlier failed turn is shown as failed with its reason instead of as an
empty reply. Worker leases, run keys and attempt counts stay internal.
"""

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime
from tests.support.task_execution import run_task

PUBLIC_TURN_FIELDS = {
    "turn_id",
    "task_id",
    "user_message_id",
    "assistant_message_id",
    "status",
    "error",
    "created_at",
    "started_at",
    "finished_at",
}


def _task_with_a_failed_then_a_successful_turn(client, runtime, monkeypatch):
    run_agent = runtime.task_worker_service.agent_execution.run_agent

    def fail_once(*_args, **_kwargs):
        monkeypatch.setattr(
            runtime.task_worker_service.agent_execution, "run_agent", run_agent
        )
        raise RuntimeError("internal detail")

    monkeypatch.setattr(
        runtime.task_worker_service.agent_execution, "run_agent", fail_once
    )
    task, failed, _run, _artifacts = run_task(client, runtime, "First question")
    assert failed.status.value == "failed"
    follow_up = client.post(
        f"/api/v0/tasks/{task['task_id']}/messages", json={"content": "Try again"}
    )
    assert follow_up.status_code == 202, follow_up.text
    succeeded = runtime.task_worker_service.process_next_turn("contract-test-worker")
    assert succeeded.status.value == "succeeded"
    return task, failed, succeeded


def test_turn_history_lists_every_turn_in_order_with_its_outcome(monkeypatch):
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        task, failed, succeeded = _task_with_a_failed_then_a_successful_turn(
            client, runtime, monkeypatch
        )
        response = client.get(f"/api/v0/tasks/{task['task_id']}/turns")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    first, second = body["items"]
    assert [first["turn_id"], second["turn_id"]] == [failed.turn_id, succeeded.turn_id]
    assert first["status"] == "failed"
    assert first["error"]["code"] == "task.agent_run_failed"
    assert "internal detail" not in first["error"]["summary"]
    assert first["assistant_message_id"] is None
    assert second["status"] == "succeeded"
    assert second["error"] is None
    assert second["assistant_message_id"] == succeeded.assistant_message_id
    for item in body["items"]:
        assert set(item) == PUBLIC_TURN_FIELDS  # no worker, run key or attempts


def test_turn_history_pages_newest_first_with_a_cursor(monkeypatch):
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        task, failed, succeeded = _task_with_a_failed_then_a_successful_turn(
            client, runtime, monkeypatch
        )
        path = f"/api/v0/tasks/{task['task_id']}/turns"
        newest = client.get(path, params={"limit": 1}).json()
        assert [item["turn_id"] for item in newest["items"]] == [succeeded.turn_id]
        assert newest["next_cursor"]
        older = client.get(
            path, params={"limit": 1, "cursor": newest["next_cursor"]}
        ).json()
        assert [item["turn_id"] for item in older["items"]] == [failed.turn_id]
        assert older["next_cursor"] is None


def test_turn_history_of_an_unknown_task_is_not_found():
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/v0/tasks/missing-task/turns").status_code == 404
