"""Hidden Q&A conversations and processed inbox rows expire."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modules.slack.retention import DEFAULT_QA_DAYS, SlackRetention, qa_retention_days
from tests.support.slack_ask_world import AskWorld


def _retention(
    w: AskWorld, now: datetime, days: int = DEFAULT_QA_DAYS
) -> SlackRetention:
    retention = w.runtime.slack_retention
    retention._clock = lambda: now
    retention._qa_days = days
    return retention  # type: ignore[no-any-return]


def _counts(w: AskWorld) -> dict[str, int]:
    runtime = w.runtime
    return {
        "tasks": len(runtime.tasks.list()),
        "turns": len(runtime.task_turns.list()),
        "messages": len(runtime.task_messages.list()),
        "conversations": len(runtime.threads.list()),
        "agent_runs": len(runtime.agent_runs.list()),
        "threads": len(runtime.slack_threads.list()),
    }


def test_a_conversation_is_kept_until_it_is_thirty_days_old(aworld: AskWorld) -> None:
    aworld.ask("what does idempotent mean?")
    before = _counts(aworld)
    assert all(before.values())

    _retention(aworld, datetime.now(UTC) + timedelta(days=29)).run()
    assert _counts(aworld) == before

    removed = _retention(aworld, datetime.now(UTC) + timedelta(days=31)).run()

    assert removed >= 1
    assert all(count == 0 for count in _counts(aworld).values())


def test_what_the_model_said_is_gone_with_the_conversation(aworld: AskWorld) -> None:
    aworld.open_audience()
    aworld.plan("jira.issues.search")
    aworld.world.connectors.issues = [
        {
            "key": "WEAVES-1",
            "fields": {"summary": "x", "project": {"key": "WEAVES"}},
        }
    ]
    aworld.ask("what is WEAVES-1?")
    assert aworld.runtime.tool_invocations.list() and aworld.runtime.artifacts.list()

    _retention(aworld, datetime.now(UTC) + timedelta(days=40)).run()

    assert aworld.runtime.tool_invocations.list() == ()
    assert aworld.runtime.artifacts.list() == ()
    assert aworld.runtime.agent_runs.list() == ()
    assert aworld.runtime.runs.list() == ()


def test_a_new_mention_after_expiry_starts_a_fresh_conversation(
    aworld: AskWorld,
) -> None:
    first = aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()
    aworld.reply()
    old_task = aworld.latest().task_id
    _retention(aworld, datetime.now(UTC) + timedelta(days=45)).run()

    aworld.mention("and again?", thread_ts=first.message_ts)
    aworld.dispatch()

    assert aworld.latest().task_id != old_task
    assert aworld.latest().request_count == 1


def test_a_request_still_being_answered_is_not_deleted_before_its_time(
    aworld: AskWorld,
) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()

    _retention(aworld, datetime.now(UTC) + timedelta(days=2)).run()

    assert _counts(aworld)["threads"] == 1


def test_processed_inbox_rows_go_after_seven_days_and_pending_ones_stay(
    aworld: AskWorld,
) -> None:
    aworld.mention("hello there")
    aworld.dispatch()
    aworld.mention("pending one")  # never dispatched
    assert len(aworld.inbox()) == 2

    _retention(aworld, datetime.now(UTC) + timedelta(days=8)).run()

    remaining = aworld.inbox()
    assert [e.state for e in remaining] == ["pending"]


def test_the_retention_window_is_configurable_within_bounds() -> None:
    assert qa_retention_days({}) == 30
    assert qa_retention_days({"WEAVES_SLACK_QA_RETENTION_DAYS": "7"}) == 7
    assert qa_retention_days({"WEAVES_SLACK_QA_RETENTION_DAYS": "0"}) == 30
    assert qa_retention_days({"WEAVES_SLACK_QA_RETENTION_DAYS": "x"}) == 30
