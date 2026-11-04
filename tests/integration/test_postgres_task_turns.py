from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from contracts.v1 import TaskTurn, TaskTurnStatus
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from tests.support.runtime import LocalPlatformRuntime


def test_postgres_task_turn_claim_is_atomic_across_worker_pools(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    second_store = ProductPostgresStore(isolated_postgres_url)
    first_turns = create_product_repository_set(first_store).get("task_turns")
    second_turns = create_product_repository_set(second_store).get("task_turns")
    now = datetime.now(UTC)
    first_turns.create(
        TaskTurn(
            turn_id="postgres-task-turn-1",
            task_id="postgres-task-1",
            user_message_id="postgres-task-message-1",
            requested_by_principal_id="postgres-principal",
            status=TaskTurnStatus.QUEUED,
            org_id="postgres-org",
            workspace_id="postgres-workspace",
            created_at=now,
            updated_at=now,
        )
    )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = tuple(
                pool.map(
                    lambda item: item.claim_next_queued(
                        f"worker-{id(item)}", now, None, None
                    ),
                    (first_turns, second_turns),
                )
            )
        claimed = tuple(item for item in claims if item is not None)
        assert len(claimed) == 1
        assert claimed[0].status is TaskTurnStatus.RUNNING
        assert claimed[0].attempts == 1
        assert claimed[0].worker_id is not None
    finally:
        first_store.close()
        second_store.close()


def test_postgres_task_api_and_worker_persist_a_complete_turn(
    isolated_postgres_url, monkeypatch
):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setenv(
        "WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    task = runtime.task_service.create_task(
        agent_id="local-assistant",
        title="Postgres task",
        initial_message="Summarize the local security basics.",
    )
    turn = runtime.task_worker_service.process_next_turn("postgres-task-worker")
    assert turn is not None
    assert turn.status is TaskTurnStatus.SUCCEEDED
    stored_task = runtime.task_service.get_task(task.task_id)
    assert stored_task.latest_turn_status is TaskTurnStatus.SUCCEEDED
    stale_update = stored_task.model_copy(update={"title": "Stale writer"})
    assert not runtime.tasks.put_if_task_state(
        stale_update,
        expected_status="open",
        expected_active_turn_id=turn.turn_id,
    )
    assert runtime.task_service.get_task(task.task_id).title == "Postgres task"
    messages = runtime.task_service.list_messages(task.task_id)
    assert [message.role.value for message in messages] == ["user", "assistant"]
