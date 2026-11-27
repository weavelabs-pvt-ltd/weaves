"""Delivery failures of the reply tick: backoff, undeliverable, dead token, scrubber."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modules.slack.reply import compose
from modules.slack.reply.tick import MAX_ATTEMPTS
from modules.slack.text.scrub import Scrubbed
from tests.support.slack_ask_world import AskWorld


def _public(w: AskWorld) -> list[dict]:
    return [m for m in w.slack.messages if not m.get("deleted")]


# -- delivery failures -------------------------------------------------------------


def test_a_failed_post_is_retried_with_backoff_and_lands_once(aworld: AskWorld) -> None:
    aworld.slack.faults["chat.postMessage"] = ["500", "500"]
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()
    now = datetime.now(UTC)

    aworld.at(now)
    aworld.reply()
    row = aworld.latest()
    assert row.reply_attempts == 1 and row.reply_state == "pending"
    assert row.next_attempt_at is not None and row.next_attempt_at > now

    aworld.at(now + timedelta(seconds=2))
    aworld.reply()  # too early: backing off
    assert aworld.slack.count("chat.postMessage") == 1

    aworld.at(now + timedelta(seconds=30))
    aworld.reply()  # second 500
    assert aworld.latest().reply_attempts == 2

    aworld.at(now + timedelta(minutes=5))
    aworld.reply()  # succeeds

    assert aworld.slack.count("chat.postMessage") == 3
    assert len(_public(aworld)) == 1
    assert aworld.latest().reply_state == "posted"


def test_a_rate_limit_waits_the_time_slack_asked_for(aworld: AskWorld) -> None:
    aworld.slack.faults["chat.postMessage"] = ["429"]
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()
    now = datetime.now(UTC)
    aworld.at(now)

    aworld.reply()

    row = aworld.latest()
    assert row.next_attempt_at is not None
    assert row.next_attempt_at - now >= timedelta(seconds=120)


def test_a_permanent_error_gives_up_at_once(aworld: AskWorld) -> None:
    aworld.slack.faults["chat.postMessage"] = ["channel_not_found"]
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()

    aworld.reply()
    aworld.reply()

    row = aworld.latest()
    assert row.reply_state == "undeliverable" and row.reply_error == "channel_not_found"
    assert aworld.slack.count("chat.postMessage") == 1


def test_retries_end_in_undeliverable(aworld: AskWorld) -> None:
    aworld.slack.faults["chat.postMessage"] = ["500"] * 20
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()
    moment = datetime.now(UTC)

    for _ in range(MAX_ATTEMPTS + 2):
        aworld.at(moment)
        aworld.reply()
        moment += timedelta(minutes=10)

    assert aworld.latest().reply_state == "undeliverable"
    assert aworld.slack.count("chat.postMessage") == MAX_ATTEMPTS


def test_a_dead_token_ends_the_installation_and_stops_posting(
    aworld: AskWorld,
) -> None:
    aworld.slack.faults["chat.postMessage"] = ["token_revoked"]
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()

    aworld.reply()

    held = aworld.runtime.slack_store.find_by_team("T0FAKE001")
    assert held is None  # revoked rows are no longer held
    assert aworld.latest().reply_state == "undeliverable"
    assert aworld.runtime.slack_store.list_held() == []


def test_the_scrubber_failing_closed_posts_nothing(aworld: AskWorld) -> None:
    def broken(_: str) -> Scrubbed:
        raise RuntimeError("scrubber down")

    aworld.runtime.slack_reply_tick._delivery._poster._scrubber = broken
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()

    aworld.reply()

    assert aworld.slack.count("chat.postMessage") == 0
    assert aworld.slack.ephemerals == []
    row = aworld.latest()
    assert row.reply_state == "undeliverable" and row.reply_error == "RuntimeError"


def test_a_suspended_installation_posts_no_answer(aworld: AskWorld) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()
    aworld.runtime.slack_store.set_status(
        "slack-inst-1", "suspended", datetime.now(UTC)
    )

    aworld.reply()

    assert aworld.slack.count("chat.postMessage") == 0
    assert aworld.latest().reply_state == "undeliverable"


def test_the_answer_cap_keeps_the_footer() -> None:
    text = compose.answer(
        "word " * 3000,
        titles=["A", "B", "C", "D"],
        sources=4,
        elapsed_seconds=41,
        skipped_note=None,
    )
    assert len(text) <= 3800
    assert "Read the full answer in Weaves" in text
    assert text.endswith("_4 sources · 41s_")
    assert "_From A; B; C (+1 more)_" in text
