"""Stop and Try again on PostgreSQL under the restricted application role.

The inbox table (press rows included) is closed to tenants and unscoped roles by
the generic control in ``test_postgres_slack.py``.

Concurrency and isolation that the in-process tests cannot show: parallel
deliveries of one press store one inbox row, parallel Stop presses cancel the turn
once and edit the message once, parallel Try again presses start one new turn, a
worker that lost its lease cannot write a late answer, and press rows are
invisible to every tenant and to an unscoped role.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import TaskTurnStatus
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.slack import copy
from tests.support import slack_pipeline_world
from tests.support.slack_ask_world import AskWorld, build_ask_world
from tests.support.slack_pipeline_world import ORG, USER

PRESSES = 6


@pytest.fixture
def pg_aworld(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "true")
    monkeypatch.setenv("WEAVES_SLACK_SIGNING_SECRET", "fixture-signing-secret")

    def restricted_runtime():
        pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=12)
        store = ProductPostgresStore(pool=pool, app_role="weaves_app")
        return compose_platform_runtime(http_client=httpx.Client(), store=store)

    monkeypatch.setattr(
        slack_pipeline_world, "LocalPlatformRuntime", restricted_runtime
    )
    world = build_ask_world()
    try:
        yield world
    finally:
        world.world.close()


def _running(w: AskWorld) -> None:
    w.mention("what is WEAVES-1?")
    w.dispatch()
    w.at(w.latest().requested_at + timedelta(seconds=3))
    w.force_running()
    w.reply()


def _audit(w: AskWorld) -> list:
    return [
        e
        for e in w.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.interaction"
    ]


def _parallel(fn, count: int = PRESSES) -> list:
    barrier = Barrier(count)

    def run(index: int):
        barrier.wait()
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_parallel_deliveries_of_one_press_store_one_row(pg_aworld: AskWorld) -> None:
    _running(pg_aworld)

    outcomes = _parallel(
        lambda _: pg_aworld.press("stop", action_ts="1700100500.000100")
    )

    assert sorted(outcomes) == ["accepted"] + ["duplicate"] * (PRESSES - 1)
    presses = [r for r in pg_aworld.inbox() if r.event_type == "interaction"]
    assert len(presses) == 1


def test_parallel_stop_presses_cancel_once_and_edit_once(pg_aworld: AskWorld) -> None:
    _running(pg_aworld)
    row = pg_aworld.latest()
    for _ in range(PRESSES):
        pg_aworld.press("stop")
    tick = pg_aworld.runtime.slack_interactions

    _parallel(lambda _: tick.run())

    assert pg_aworld.runtime.task_turns.get(row.turn_id).status is (
        TaskTurnStatus.CANCELLED
    )
    stored = pg_aworld.latest()
    assert stored.run_state == "stopped" and stored.reply_state == "posted"
    assert [m["text"] for m in pg_aworld.slack.messages] == [copy.STOPPED]
    assert pg_aworld.slack.count("chat.update") == 1
    assert pg_aworld.slack.count("chat.postMessage") == 1
    # Every press is audited once; exactly one of them did the stopping.
    events = _audit(pg_aworld)
    assert len(events) == PRESSES
    reasons = sorted(e.metadata["reason"] for e in events)
    assert reasons == ["already_stopped"] * (PRESSES - 1) + ["requester"]
    assert sorted(t["text"] for t in pg_aworld.slack.ephemerals) == [
        copy.ALREADY_STOPPED
    ] * (PRESSES - 1)


def test_a_worker_that_lost_the_turn_cannot_write_a_late_answer(
    pg_aworld: AskWorld,
) -> None:
    _running(pg_aworld)
    held = pg_aworld.runtime.task_turns.get(pg_aworld.latest().turn_id)
    assert held.worker_id == "test-worker"

    pg_aworld.press("stop")
    pg_aworld.interact()

    stopped = pg_aworld.runtime.task_turns.get(held.turn_id)
    assert stopped.status is TaskTurnStatus.CANCELLED and stopped.worker_id is None
    late = held.model_copy(update={"status": TaskTurnStatus.SUCCEEDED})
    assert not pg_aworld.runtime.task_turns.put_if_worker_owned(
        late, worker_id="test-worker", attempts=held.attempts
    )
    assert (
        pg_aworld.runtime.task_turns.get(held.turn_id).status
        is TaskTurnStatus.CANCELLED
    )


def test_parallel_try_again_presses_start_one_new_turn(
    pg_aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = pg_aworld.runtime.agent_execution_service.run_agent

    def broken(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(pg_aworld.runtime.agent_execution_service, "run_agent", broken)
    pg_aworld.ask("what does idempotent mean?")
    monkeypatch.setattr(
        pg_aworld.runtime.agent_execution_service, "run_agent", original
    )
    failed = pg_aworld.latest()
    for index in range(PRESSES):
        pg_aworld.press(
            "retry",
            ref=failed.run_ref,
            message_ts=failed.answer_ts,
            action_ts=f"1700100{600 + index}.000100",
        )

    _parallel(lambda _: pg_aworld.interact())

    stored = pg_aworld.latest()
    assert stored.retries == 1 and stored.run_state == "queued"
    assert len(pg_aworld.runtime.task_turns.list()) == 2
    answered = {t["text"] for t in pg_aworld.slack.ephemerals}
    assert answered <= {copy.NO_LONGER_AVAILABLE, copy.ALREADY_RETRYING}
    assert len(_audit(pg_aworld)) == PRESSES


def test_the_press_row_loses_its_reference_once_settled(pg_aworld: AskWorld) -> None:
    _running(pg_aworld)
    pg_aworld.press("stop")
    (pending,) = [
        r for r in pg_aworld.inbox("pending") if r.event_type == "interaction"
    ]
    assert pending.text == pg_aworld.latest().run_ref

    pg_aworld.interact()

    (done,) = [r for r in pg_aworld.inbox("done") if r.event_type == "interaction"]
    assert done.text is None and done.user_id == USER
