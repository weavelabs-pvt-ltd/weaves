"""``@Weaves task:`` on PostgreSQL under the restricted application role.

The same world as the unit tests (fake Slack Web API, counting connectors, fake
model) over real tables with row-level security: one task per event under
concurrent dispatch, one task per thread under concurrent *different* events, the
result posted once under concurrent reply ticks, Execute limits derived from the
stored audit events, the origin filter in SQL, tenant isolation of the thread rows
and a created task that survives the retention sweep.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from shared.request_context import reset_request_principal, set_request_identity
from tests.support import slack_pipeline_world
from tests.support.slack_ask_world import AskWorld, build_ask_world
from tests.support.slack_pipeline_world import ORG
from tests.support.slack_task_world import (
    APP_URL,
    MEMBER,
    TASK_TEXT,
    enable_tasks,
    make_agent,
    run_task,
    task_rows,
    tasks,
)


@pytest.fixture
def pg_aworld(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "true")
    monkeypatch.setenv("WEAVES_SLACK_SIGNING_SECRET", "fixture-signing-secret")
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", APP_URL)

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


def _decisions(w: AskWorld, action: str) -> list:
    return [e for e in w.runtime.audit_events.list_scoped(ORG) if e.action == action]


def test_a_task_runs_end_to_end_under_row_security(pg_aworld: AskWorld) -> None:
    enable_tasks(pg_aworld)

    run_task(pg_aworld)

    (task,) = tasks(pg_aworld)
    assert task.created_by_principal_id == MEMBER and task.origin is not None
    first, result = pg_aworld.slack.messages
    assert first["text"].startswith(":white_check_mark: Task done: ")
    assert result["text"].endswith("Full result and sources in Weaves.")
    (row,) = task_rows(pg_aworld)
    assert (row.reply_state, row.run_state, row.posts) == ("posted", "done", 2)
    assert len(_decisions(pg_aworld, "slack.execute.allowed")) == 1


def test_concurrent_dispatchers_make_one_task_for_one_event(
    pg_aworld: AskWorld,
) -> None:
    enable_tasks(pg_aworld)
    event = pg_aworld.mention(f"task: {TASK_TEXT}")
    dispatcher = pg_aworld.runtime.slack_dispatcher
    barrier = Barrier(6)

    def handle(_: int) -> None:
        barrier.wait()
        dispatcher._handle(event)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(handle, range(6)))

    assert len(tasks(pg_aworld)) == 1
    assert len(pg_aworld.runtime.task_turns.list()) == 1
    assert len(task_rows(pg_aworld)) == 1
    assert len(_decisions(pg_aworld, "slack.execute.allowed")) == 1  # counted once
    assert pg_aworld.slack.ephemerals == []  # nobody was refused


def test_different_events_in_one_thread_make_one_task(pg_aworld: AskWorld) -> None:
    enable_tasks(pg_aworld)
    root = "1699990000.000100"
    events = [
        pg_aworld.mention(f"task: do thing {n}", thread_ts=root) for n in range(6)
    ]
    dispatcher = pg_aworld.runtime.slack_dispatcher
    barrier = Barrier(len(events))

    def handle(index: int) -> None:
        barrier.wait()
        dispatcher._handle(events[index])

    with ThreadPoolExecutor(max_workers=len(events)) as pool:
        list(pool.map(handle, range(len(events))))

    assert len(tasks(pg_aworld)) == 1 and len(task_rows(pg_aworld)) == 1
    texts = [m["text"] for m in pg_aworld.slack.messages]
    assert len(texts) == 5 and all(t.startswith("Already tracked") for t in texts)


def test_concurrent_reply_ticks_post_each_message_once(pg_aworld: AskWorld) -> None:
    enable_tasks(pg_aworld)
    pg_aworld.mention(f"task: {TASK_TEXT}")
    pg_aworld.dispatch()
    pg_aworld.work()
    barrier = Barrier(5)

    def tick(_: int) -> int:
        barrier.wait()
        return pg_aworld.reply()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(tick, range(5)))

    assert pg_aworld.slack.count("chat.postMessage") == 2  # created + result
    assert task_rows(pg_aworld)[0].reply_state == "posted"


def test_the_execute_limit_is_derived_from_the_stored_audit(
    pg_aworld: AskWorld,
) -> None:
    enable_tasks(
        pg_aworld, execute_limits={"user_per_10_min": 2, "channel_per_10_min": 30}
    )
    for n in range(3):
        pg_aworld.mention(f"task: job {n}", thread_ts=f"1699990{n:03d}.000100")
    pg_aworld.dispatch()

    assert len(tasks(pg_aworld)) == 2
    assert "try again in" in pg_aworld.slack.ephemerals[0]["text"]
    later = datetime.now(UTC) + timedelta(minutes=11)
    pg_aworld.runtime.slack_dispatcher._clock = lambda: later
    pg_aworld.mention("task: after the window", thread_ts="1699991000.000100")
    pg_aworld.dispatch()
    assert len(tasks(pg_aworld)) == 3


def test_the_origin_filter_and_hidden_tasks_in_sql(pg_aworld: AskWorld) -> None:
    enable_tasks(pg_aworld)
    pg_aworld.runtime.task_service.create_task(
        agent_id=make_agent(pg_aworld, name="Other"),
        title="A dashboard task",
        initial_message="hello",
    )
    pg_aworld.ask("what does idempotent mean?")  # a hidden Q&A task
    pg_aworld.mention(f"task: {TASK_TEXT}")
    pg_aworld.dispatch()

    service = pg_aworld.runtime.task_service
    assert {t.title for t in service.list_tasks()} == {"A dashboard task", TASK_TEXT}
    assert [t.title for t in service.list_tasks(origin_kind="slack")] == [TASK_TEXT]
    (slack_task,) = service.list_tasks(origin_kind="slack")
    assert slack_task.origin is not None and slack_task.origin.kind == "slack"


def test_task_thread_rows_are_invisible_to_another_tenant(pg_aworld: AskWorld) -> None:
    enable_tasks(pg_aworld)
    run_task(pg_aworld)
    (row,) = task_rows(pg_aworld)
    runtime = pg_aworld.runtime

    token = set_request_identity("nobody", "unrelated-org", "ws")
    try:
        assert runtime.slack_threads.list() == ()
        with pytest.raises(KeyError):
            runtime.slack_threads.get_scoped(row.id, ORG)
        assert runtime.tasks.list() == ()
    finally:
        reset_request_principal(token)
    assert runtime.slack_threads.get_scoped(row.id, ORG).kind == "task"


def test_the_thread_key_separates_a_task_from_an_ask_in_one_thread(
    pg_aworld: AskWorld,
) -> None:
    enable_tasks(pg_aworld)
    root = "1699990000.000100"
    pg_aworld.mention(f"task: {TASK_TEXT}", thread_ts=root)
    pg_aworld.mention("what does idempotent mean?", thread_ts=root)
    pg_aworld.dispatch()

    kinds = sorted(r.kind for r in pg_aworld.threads())
    assert kinds == ["ask", "task"]  # unique per (thread, kind)
    row = task_rows(pg_aworld)[0]
    with pytest.raises(ValueError):
        pg_aworld.runtime.slack_threads.create(row.model_copy(update={"id": "other"}))


def test_retention_keeps_the_created_task_and_its_row(pg_aworld: AskWorld) -> None:
    enable_tasks(pg_aworld)
    run_task(pg_aworld)
    retention = pg_aworld.runtime.slack_retention
    retention._clock = lambda: datetime.now(UTC) + timedelta(days=90)

    retention.run()

    assert len(tasks(pg_aworld)) == 1 and len(task_rows(pg_aworld)) == 1
