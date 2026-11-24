from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from application.ports.usage import UsageSettlement
from application.runtime_constants import (
    STALE_RUN_AFTER_SECONDS,
    STALE_TASK_TURN_AFTER_SECONDS,
)
from contracts.v1 import TaskTurn, TaskTurnStatus, UsageGroupBy, UsageState
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from shared.request_context import system_scope
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


class SimulatedCrash(BaseException):
    """The worker process dies here; no ``except Exception`` may absorb it."""


def test_postgres_killed_turn_reservation_is_unknown_at_the_turn_lease(
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
        agent_id="local-assistant", title="Killed", initial_message="hello"
    )

    def dies(*args, **kwargs):
        raise SimulatedCrash

    with monkeypatch.context() as crashed:
        crashed.setattr(runtime.usage_ledger, "settle", dies)
        crashed.setattr(runtime.usage_service, "settle", dies)
        with pytest.raises(SimulatedCrash):
            runtime.task_worker_service.process_next_turn("worker-killed")
    (turn,) = runtime.task_turns.list_scoped(task.org_id, task.workspace_id)
    assert turn.status is TaskTurnStatus.RUNNING
    reconciler = runtime.usage_reconciler

    def reconcile_at(moment):
        reconciler._clock = lambda: moment
        return reconciler.reconcile()

    with system_scope("usage"):
        (reserved,) = runtime.usage_ledger.list_reserved_before(
            turn.updated_at + timedelta(days=1), limit=10
        )
    assert reserved.state is UsageState.RESERVED
    key = (reserved.org_id, reserved.reservation_key)
    fence = STALE_TASK_TURN_AFTER_SECONDS
    assert reconcile_at(turn.updated_at + timedelta(seconds=fence - 10)) == 0
    expired = turn.updated_at + timedelta(seconds=fence + 5)
    assert fence + 5 < STALE_RUN_AFTER_SECONDS
    assert reconcile_at(expired) == 1
    assert reconcile_at(expired) == 0
    row = runtime.usage_ledger.get(*key)
    assert row.state is UsageState.UNKNOWN and row.outcome == "lease_expired"
    day = expired.date()
    totals = runtime.usage_ledger.summary(key[0], day, day, UsageGroupBy.DAY)[0]
    assert totals.total_tokens == 0
    late = UsageSettlement(
        org_id=row.org_id,
        workspace_id=row.workspace_id,
        reservation_key=row.reservation_key,
        agent_run_id=row.agent_run_id,
        run_id=row.run_id,
        agent_id=row.agent_id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        input_tokens=21,
        output_tokens=8,
        estimated_cost_nano_usd=None,
        outcome="late",
        now=expired,
    )
    assert runtime.usage_ledger.settle(late) is True
    assert runtime.usage_ledger.settle(late) is False
    assert runtime.usage_ledger.get(*key).state is UsageState.SETTLED
