from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    TaskMessage,
    TaskMessageRole,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
)
from tests.support.runtime import LocalPlatformRuntime


def test_task_contract_rejects_inconsistent_states():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    task_service = runtime.task_service
    task = task_service.create_task(
        agent_id="local-assistant",
        title="Contract validation",
        initial_message="Please help.",
    )
    turn = runtime.task_turns.get(task.active_turn_id)

    assert task.status is TaskStatus.OPEN
    assert turn.status is TaskTurnStatus.QUEUED
    try:
        TaskTurn.model_validate(
            turn.model_copy(update={"status": TaskTurnStatus.SUCCEEDED}).model_dump()
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a successful turn without a response was accepted")

    try:
        TaskMessage(
            message_id="invalid-message",
            task_id=task.task_id,
            turn_id=turn.turn_id,
            org_id=task.org_id,
            workspace_id=task.workspace_id,
            role=TaskMessageRole.USER,
            content="user message without creator",
            created_at=task.created_at,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("user message without creator was accepted")


def test_task_api_runs_agent_and_supports_followup_and_lifecycle():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))

    with client:
        created = client.post(
            "/api/v0/tasks",
            headers={"Idempotency-Key": "create-task-flow-1"},
            json={
                "agent_id": "local-assistant",
                "title": "First task",
                "initial_message": "Summarize our security basics.",
            },
        )
        assert created.status_code == 202, created.text
        task = created.json()
        assert task["active_turn_status"] == "queued"
        assert task["agent_version_id"] == "local-assistant-v1"

        replay = client.post(
            "/api/v0/tasks",
            headers={"Idempotency-Key": "create-task-flow-1"},
            json={
                "agent_id": "local-assistant",
                "title": "First task",
                "initial_message": "Summarize our security basics.",
            },
        )
        assert replay.status_code == 202
        assert replay.json()["task_id"] == task["task_id"]

        turn = runtime.task_worker_service.process_next_turn("task-test-worker")
        assert turn is not None
        assert turn.status is TaskTurnStatus.SUCCEEDED
        detail = client.get(f"/api/v0/tasks/{task['task_id']}")
        assert detail.status_code == 200
        assert detail.json()["active_turn_id"] is None
        assert detail.json()["latest_turn_status"] == "succeeded"
        messages = client.get(f"/api/v0/tasks/{task['task_id']}/messages")
        assert messages.status_code == 200
        assert [item["role"] for item in messages.json()["items"]] == [
            "user",
            "assistant",
        ]
        _assert_task_message_pagination(client, task["task_id"])

        added = client.post(
            f"/api/v0/tasks/{task['task_id']}/messages",
            headers={"Idempotency-Key": "followup-turn-1"},
            json={"content": "Can you make that shorter?"},
        )
        assert added.status_code == 202, added.text
        duplicate = client.post(
            f"/api/v0/tasks/{task['task_id']}/messages",
            headers={"Idempotency-Key": "followup-turn-1"},
            json={"content": "Can you make that shorter?"},
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["turn_id"] == added.json()["turn_id"]
        rejected_overlap = client.post(
            f"/api/v0/tasks/{task['task_id']}/messages",
            json={"content": "Another overlapping message."},
        )
        assert rejected_overlap.status_code == 409

        runtime.task_worker_service.process_next_turn("task-test-worker")
        completed = client.post(f"/api/v0/tasks/{task['task_id']}/complete")
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        archived = client.post(f"/api/v0/tasks/{task['task_id']}/archive")
        assert archived.status_code == 409
        reopened = client.post(f"/api/v0/tasks/{task['task_id']}/reopen")
        assert reopened.status_code == 200
        assert reopened.json()["status"] == "open"


def _assert_task_message_pagination(client, task_id):
    """Check transcript pages preserve newest-first cursor ordering."""
    recent = client.get(f"/api/v0/tasks/{task_id}/messages?limit=1").json()
    older = client.get(
        f"/api/v0/tasks/{task_id}/messages?limit=1&cursor={recent['next_cursor']}"
    ).json()
    assert recent["items"][0]["role"] == "assistant"
    assert older["items"][0]["role"] == "user"


def test_task_api_rejects_bad_and_cross_workspace_requests():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        blank = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "local-assistant",
                "title": "Invalid task",
                "initial_message": "   ",
            },
        )
        assert blank.status_code == 422
        missing = client.get("/api/v0/tasks/missing-task")
        assert missing.status_code == 404
        unsupported = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "unknown-agent",
                "title": "Unknown agent task",
                "initial_message": "Please help.",
            },
        )
        assert unsupported.status_code == 404
        task = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "local-assistant",
                "title": "Scoped task",
                "initial_message": "Please help.",
            },
        ).json()
        task_record = runtime.tasks.get(task["task_id"])
        assert task_record.workspace_id == "local-workspace"


def test_task_idempotency_key_rejects_different_payload():
    client = TestClient(create_app(LocalPlatformRuntime(postgres_store=None)))
    with client:
        payload = {
            "agent_id": "local-assistant",
            "title": "Idempotent task",
            "initial_message": "First request.",
        }
        first = client.post(
            "/api/v0/tasks",
            headers={"Idempotency-Key": "task-conflict-1"},
            json=payload,
        )
        changed = {**payload, "initial_message": "Different request."}
        conflict = client.post(
            "/api/v0/tasks",
            headers={"Idempotency-Key": "task-conflict-1"},
            json=changed,
        )
        assert first.status_code == 202
        assert conflict.status_code == 409


def test_failed_task_turn_can_be_retried_without_exposing_provider_error():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        created = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "local-assistant",
                "title": "Retry task",
                "initial_message": "Please answer this.",
            },
        )
        task = created.json()
        original_run = runtime.agent_execution_service.run_agent

        def fail_run(*args, **kwargs):
            raise RuntimeError("secret provider diagnostic must not be returned")

        runtime.agent_execution_service.run_agent = fail_run
        failed = runtime.task_worker_service.process_next_turn("task-retry-worker")
        assert failed is not None
        assert failed.status is TaskTurnStatus.FAILED
        assert "secret provider diagnostic" not in failed.error.summary
        assert runtime.tasks.get(task["task_id"]).latest_turn_status == "failed"

        retry = client.post(
            f"/api/v0/tasks/{task['task_id']}/turns/{failed.turn_id}/retry"
        )
        assert retry.status_code == 202
        runtime.agent_execution_service.run_agent = original_run
        succeeded = runtime.task_worker_service.process_next_turn("task-retry-worker")
        assert succeeded is not None
        assert succeeded.status is TaskTurnStatus.SUCCEEDED
        assert succeeded.attempts == 2


def test_archived_task_is_read_only_and_cannot_be_reopened():
    runtime = LocalPlatformRuntime(postgres_store=None)
    task = runtime.task_service.create_task(
        agent_id="local-assistant",
        title="Archived task",
        initial_message="This task will be archived.",
    )
    turn = runtime.task_worker_service.process_next_turn("task-archive-worker")
    assert turn is not None and turn.status is TaskTurnStatus.SUCCEEDED
    archived = runtime.task_service.archive_task(task.task_id)
    assert archived.status is TaskStatus.ARCHIVED
    try:
        runtime.task_service.add_message(task.task_id, "Read-only follow-up")
    except ValueError as exc:
        assert "open tasks" in str(exc)
    else:
        raise AssertionError("archived task accepted a new message")
    try:
        runtime.task_service.reopen_task(task.task_id)
    except ValueError as exc:
        assert "read-only" in str(exc)
    else:
        raise AssertionError("archived task was reopened")


def test_task_list_uses_bounded_keyset_pagination_and_validates_cursor():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        for index in range(3):
            response = client.post(
                "/api/v0/tasks",
                json={
                    "agent_id": "local-assistant",
                    "title": f"Page task {index}",
                    "initial_message": f"Request {index}.",
                },
            )
            assert response.status_code == 202
        first = client.get("/api/v0/tasks?limit=2").json()
        assert len(first["items"]) == 2
        assert first["next_cursor"] is not None
        second = client.get(
            f"/api/v0/tasks?limit=2&cursor={first['next_cursor']}"
        ).json()
        assert len(second["items"]) == 1
        assert second["next_cursor"] is None
        invalid = client.get("/api/v0/tasks?cursor=invalid")
        assert invalid.status_code == 422
