"""The Slack Ask flow on PostgreSQL under the restricted application role.

The same world as the unit tests (counting fake connectors, fake model, fake Slack
Web API), but over real tables with row-level security: one task per event under
concurrent dispatch, exactly one post under concurrent reply ticks, the thread
rows isolated by tenant, the unique thread key, and retention.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import httpx
import pytest
from cryptography.fernet import Fernet

from application.ports.slack import InboxEvent
from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from shared.request_context import reset_request_principal, set_request_identity
from tests.support import slack_pipeline_world
from tests.support.slack_ask_world import AskWorld, build_ask_world
from tests.support.slack_pipeline_world import CHANNEL, ORG, TEAM


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


def test_a_full_ask_is_answered_once_under_row_security(pg_aworld: AskWorld) -> None:
    pg_aworld.ask("what does idempotent mean?")

    (message,) = pg_aworld.slack.messages
    assert "From general knowledge" in message["text"]
    row = pg_aworld.latest()
    assert row.reply_state == "posted" and row.posts == 1
    (inbox,) = pg_aworld.inbox()
    assert inbox.state == "done" and inbox.text is None


def test_concurrent_dispatchers_make_one_task_for_one_event(
    pg_aworld: AskWorld,
) -> None:
    event = pg_aworld.mention("what does idempotent mean?")
    dispatcher = pg_aworld.runtime.slack_dispatcher
    barrier = Barrier(6)

    def handle(_: int) -> None:
        barrier.wait()
        dispatcher._handle(event)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(handle, range(6)))

    assert len(pg_aworld.runtime.tasks.list()) == 1
    assert len(pg_aworld.runtime.task_turns.list()) == 1
    assert len(pg_aworld.threads()) == 1
    assert pg_aworld.slack.ephemerals == []  # nobody was refused or told "busy"


def test_concurrent_reply_ticks_post_the_answer_once(pg_aworld: AskWorld) -> None:
    pg_aworld.mention("what does idempotent mean?")
    pg_aworld.dispatch()
    pg_aworld.work()
    barrier = Barrier(5)

    def tick(_: int) -> int:
        barrier.wait()
        return pg_aworld.reply()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(tick, range(5)))

    assert pg_aworld.slack.count("chat.postMessage") == 1
    assert pg_aworld.latest().reply_state == "posted"


def test_the_inbox_row_is_settled_by_exactly_one_caller(pg_aworld: AskWorld) -> None:
    pg_aworld.mention("hello there")
    store = pg_aworld.runtime.slack_store
    event_id = pg_aworld.inbox()[0].event_id
    barrier = Barrier(12)

    def settle(_: int) -> bool:
        barrier.wait()
        return store.settle_event(event_id, "done")

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(settle, range(12)))

    assert results.count(True) == 1
    assert store.list_events(state="pending", event_type="app_mention") == []
    assert store.list_events(state="done", event_type="reaction_added") == []


def test_thread_rows_are_invisible_to_another_tenant(pg_aworld: AskWorld) -> None:
    pg_aworld.ask("what does idempotent mean?")
    row = pg_aworld.latest()
    runtime = pg_aworld.runtime

    token = set_request_identity("nobody", "unrelated-org", "ws")
    try:
        assert runtime.slack_threads.list() == ()
        assert runtime.slack_threads.list_by_field("reply_state", "posted") == ()
        with pytest.raises(KeyError):
            runtime.slack_threads.get_scoped(row.id, ORG)
        with pytest.raises(KeyError):
            runtime.slack_threads.get_scoped(row.id, "unrelated-org")
        assert (
            runtime.slack_threads.list_before(
                "updated_at", datetime.now(UTC) + timedelta(days=1), limit=10
            )
            == ()
        )
    finally:
        reset_request_principal(token)
    assert runtime.slack_threads.get_scoped(row.id, ORG).id == row.id


def test_the_thread_key_is_unique_in_the_database(pg_aworld: AskWorld) -> None:
    pg_aworld.ask("what does idempotent mean?")
    row = pg_aworld.latest()

    # A different record id with the same (org, team, channel, thread, kind) key.
    with pytest.raises(ValueError):
        pg_aworld.runtime.slack_threads.create(row.model_copy(update={"id": "another"}))
    # The same record id is refused too.
    with pytest.raises(ValueError):
        pg_aworld.runtime.slack_threads.create(row)


def test_two_mentions_of_one_message_under_different_events_make_one_row(
    pg_aworld: AskWorld,
) -> None:
    first = pg_aworld.mention("what does idempotent mean?")
    twin = InboxEvent(
        event_id="Ev0TWIN",
        team_id=TEAM,
        event_type="app_mention",
        received_at=datetime.now(UTC),
        channel_id=CHANNEL,
        user_id=first.user_id,
        message_ts=first.message_ts,
        text=first.text,
    )

    assert pg_aworld.runtime.slack_store.record_event(twin) is False


def test_retention_deletes_the_conversation_and_leaves_other_rows(
    pg_aworld: AskWorld,
) -> None:
    pg_aworld.ask("what does idempotent mean?")
    runtime = pg_aworld.runtime
    retention = runtime.slack_retention
    retention._clock = lambda: datetime.now(UTC) + timedelta(days=45)

    assert retention.run() >= 1

    assert runtime.slack_threads.list() == ()
    assert runtime.tasks.list() == ()
    assert runtime.task_turns.list() == ()
    assert runtime.agent_runs.list() == ()
    # The service principal and its agent are not conversation data.
    assert runtime.slack_access_service.qa_ids()[0] is not None
