"""The Slack PR review on PostgreSQL under the restricted application role.

The same world as the unit tests (fake GitHub over a mock transport, scripted
review model, fake Slack Web API) over real tables with row-level security: one
task per event under concurrent dispatch, exactly one review post under
concurrent reply ticks, review rows isolated by tenant and distinct from an Ask
row of the same thread, the reader's counts stored with the invocation, a
blocked review that creates nothing, and retention that never deletes the task.
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
from modules.slack.access import messages
from shared.request_context import reset_request_principal, set_request_identity
from tests.support import slack_pipeline_world
from tests.support.slack_pipeline_world import ORG
from tests.support.slack_review_world import MEMBER, ReviewWorld, build_review_world


@pytest.fixture
def pg_rworld(isolated_postgres_url, monkeypatch):
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
    world = build_review_world()
    # The local owner has no linked account in these records: make it a member.
    world.runtime.slack_access_service.allowlists.members.is_active = lambda _p: True
    try:
        yield world
    finally:
        world.aworld.world.close()


def _go(world: ReviewWorld, number: int = 123) -> None:
    world.allow()
    world.review(number)
    world.aworld.dispatch()
    world.aworld.work()
    world.aworld.reply()


def test_a_full_review_is_delivered_once_under_row_security(
    pg_rworld: ReviewWorld,
) -> None:
    _go(pg_rworld)

    (message,) = pg_rworld.aworld.slack.messages
    assert message["text"].startswith("*Review of acme/api#123*")
    (row,) = pg_rworld.aworld.threads()
    assert (row.kind, row.tier, row.reply_state) == ("review", "execute", "posted")
    (task,) = pg_rworld.tasks()
    assert task.created_by_principal_id == MEMBER and task.audience.value == "private"
    (invocation,) = [
        i
        for i in pg_rworld.runtime.tool_invocations.list()
        if i.capability_id == "github.pr.review_context"
    ]
    assert invocation.output_summary["stats"]["files_total"] == 14


def test_concurrent_dispatchers_make_one_review_task_for_one_event(
    pg_rworld: ReviewWorld,
) -> None:
    pg_rworld.allow()
    event = pg_rworld.review(123)
    dispatcher = pg_rworld.runtime.slack_dispatcher
    barrier = Barrier(6)

    def handle(_: int) -> None:
        barrier.wait()
        dispatcher._handle(event)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(handle, range(6)))

    assert len(pg_rworld.tasks()) == 1
    assert len(pg_rworld.runtime.task_turns.list()) == 1
    assert len(pg_rworld.aworld.threads()) == 1
    assert pg_rworld.aworld.slack.ephemerals == []


def test_concurrent_reply_ticks_post_the_review_once(pg_rworld: ReviewWorld) -> None:
    pg_rworld.allow()
    pg_rworld.review(123)
    pg_rworld.aworld.dispatch()
    pg_rworld.aworld.work()
    barrier = Barrier(5)

    def tick(_: int) -> int:
        barrier.wait()
        return pg_rworld.aworld.reply()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(tick, range(5)))

    assert pg_rworld.aworld.slack.count("chat.postMessage") == 1
    assert pg_rworld.aworld.threads()[0].reply_state == "posted"


def test_a_blocked_review_creates_nothing_under_row_security(
    pg_rworld: ReviewWorld,
) -> None:
    pg_rworld.allow(repos=("acme/unrelated",))
    pg_rworld.review(123)
    pg_rworld.aworld.dispatch()

    assert pg_rworld.tasks() == [] and pg_rworld.aworld.threads() == []
    assert pg_rworld.aworld.slack.ephemerals[-1]["text"] == (
        f":lock: {messages.REPO_NOT_ENABLED}"
    )


def test_a_review_and_an_ask_keep_separate_rows_in_one_thread(
    pg_rworld: ReviewWorld,
) -> None:
    pg_rworld.allow()
    first = pg_rworld.aworld.mention("what does idempotent mean?")
    pg_rworld.aworld.dispatch()
    pg_rworld.review(123, thread_ts=first.message_ts)
    pg_rworld.aworld.dispatch()

    rows = pg_rworld.aworld.threads()
    assert sorted(r.kind for r in rows) == ["ask", "review"]
    assert len({r.id for r in rows}) == 2 and all(r.id.endswith(r.kind) for r in rows)


def test_review_rows_are_invisible_to_another_tenant(pg_rworld: ReviewWorld) -> None:
    _go(pg_rworld)
    (row,) = pg_rworld.aworld.threads()
    runtime = pg_rworld.runtime

    token = set_request_identity("nobody", "unrelated-org", "ws")
    try:
        assert runtime.slack_threads.list() == ()
        with pytest.raises(KeyError):
            runtime.slack_threads.get_scoped(row.id, ORG)
        assert runtime.tool_invocations.list() == ()
    finally:
        reset_request_principal(token)
    assert runtime.slack_threads.get_scoped(row.id, ORG).id == row.id


def test_retention_drops_the_thread_map_of_an_old_review_and_keeps_the_task(
    pg_rworld: ReviewWorld,
) -> None:
    _go(pg_rworld)
    (task,) = pg_rworld.tasks()
    retention = pg_rworld.runtime.slack_retention
    retention._clock = lambda: datetime.now(UTC) + timedelta(days=40)

    assert retention.run() == 1

    assert pg_rworld.aworld.threads() == []
    assert pg_rworld.runtime.tasks.get(task.task_id).task_id == task.task_id
    assert sorted(m.role.value for m in pg_rworld.runtime.task_messages.list()) == [
        "assistant",
        "user",
    ]
