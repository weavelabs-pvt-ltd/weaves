"""The reply tick: answers, refusals, failures and the working message (scenes A-I)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1 import TaskTurnStatus
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.reply import compose
from tests.support.slack_ask_world import BOT_TOKEN, AskWorld
from tests.support.slack_pipeline_world import JIRA, USER

ISSUE = {
    "key": "WEAVES-1",
    "fields": {
        "summary": "Checkout outage",
        "project": {"key": "WEAVES"},
        "status": {"name": "Open"},
        "issuetype": {"name": "Incident"},
    },
}


def _public(w: AskWorld) -> list[dict]:
    return [m for m in w.slack.messages if not m.get("deleted")]


# -- scene A: the answer ---------------------------------------------------------


def test_an_answer_is_a_new_thread_message_with_evidence_and_receipt_line(
    aworld: AskWorld,
) -> None:
    aworld.open_audience()
    aworld.plan(JIRA)
    aworld.world.connectors.issues = [ISSUE]

    aworld.ask("what is WEAVES-1?")

    (message,) = _public(aworld)
    text = message["text"]
    assert "_From WEAVES-1: Checkout outage_" in text
    assert "_1 source · " in text and text.rstrip().endswith("s_")
    assert message["thread_ts"] == aworld.latest().thread_ts
    # Plain text only: no blocks, nothing for Slack to parse, no unfurling, no button.
    assert message["parse"] == "none" and message["unfurl_links"] == "false"
    assert "blocks" not in message and "View" not in text
    row = aworld.latest()
    assert row.reply_state == "posted" and row.run_state == "done"
    assert row.answer_ts == message["ts"] and row.posts == 1


def test_a_model_only_answer_says_general_knowledge(aworld: AskWorld) -> None:
    aworld.ask("what does idempotent mean?")

    (message,) = _public(aworld)
    assert "_From general knowledge · " in message["text"]
    assert "source" not in message["text"].split("\n")[-1]


def test_the_answer_is_posted_exactly_once_across_ticks_and_restarts(
    aworld: AskWorld,
) -> None:
    aworld.ask("what does idempotent mean?")
    aworld.reply()
    aworld.reply()
    from apps.runtime_services_slack_dispatch import attach_slack_dispatch

    attach_slack_dispatch(aworld.runtime, aworld.slack.client())  # a restarted worker
    aworld.reply()

    assert aworld.slack.count("chat.postMessage") == 1


def test_two_workers_replying_at_once_post_once(aworld: AskWorld) -> None:
    from concurrent.futures import ThreadPoolExecutor

    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: aworld.reply(), range(4)))

    assert aworld.slack.count("chat.postMessage") == 1


def test_secrets_and_mentions_never_leave_even_when_the_answer_echoes_them(
    aworld: AskWorld,
) -> None:
    aworld.ask("repeat <!channel> and <@U0VICTIM> and xoxb-1234567890-abcdefghijkl")

    (message,) = _public(aworld)
    text = message["text"]
    assert "xoxb-1234567890" not in text
    assert "<!channel>" not in text and "<@U0VICTIM>" not in text
    assert BOT_TOKEN not in json.dumps(aworld.slack.calls)


# -- scene B, E, I: refusals are private ------------------------------------------


def test_a_refusal_is_private_and_nothing_is_posted_in_the_channel(
    aworld: AskWorld,
) -> None:
    assert aworld.world.jira is not None
    aworld.world.snapshot(aworld.world.jira, "healthy")
    aworld.plan(JIRA)  # Jira keeps its default audience: not in Slack

    aworld.ask("what is WEAVES-1?")

    (whisper,) = aworld.slack.ephemerals
    assert whisper["text"] == (
        ":lock: Jira isn't open to everyone in Slack. Ask a Weaves admin."
    )
    assert whisper["user"] == USER and whisper["thread_ts"]
    assert _public(aworld) == []
    row = aworld.latest()
    assert row.reply_state == "posted" and row.private_sent and row.posts == 0


def test_a_failed_plan_asks_to_try_again_privately(aworld: AskWorld) -> None:
    aworld.open_audience()
    aworld.world.models.reply = lambda _: "not json at all"

    aworld.ask("what is going on?")

    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.PLANNER_FAILED]
    assert _public(aworld) == []
    assert aworld.world.models.answer_calls == 0


def test_partial_mode_answers_and_names_what_was_skipped(aworld: AskWorld) -> None:
    aworld.open_audience()
    aworld.world.set_slack_settings(
        partial_answers=True,
        connector_audience={
            aworld.world.jira.plugin_installation_id: "everyone",
            aworld.world.slack.plugin_installation_id: "not_in_slack",
        },
    )
    aworld.plan(JIRA, "slack.messages.search")
    aworld.world.connectors.issues = [ISSUE]

    aworld.ask("what is WEAVES-1 and what did people say?")

    (message,) = _public(aworld)
    assert "_Skipped: Slack search (not open here)_" in message["text"]
    assert "_From WEAVES-1: Checkout outage_" in message["text"]


def test_an_answer_for_allowed_people_goes_only_to_the_asker(
    aworld: AskWorld,
) -> None:
    aworld.open_audience()
    aworld.world.set_slack_settings(
        connector_audience={
            aworld.world.jira.plugin_installation_id: "allowed_people",
            aworld.world.slack.plugin_installation_id: "everyone",
        },
        rules={
            "create_task": "off",
            "pr_review": "off",
            "run_workflow": "off",
            "ask_private": "user_or_channel",
        },
    )
    aworld.runtime.slack_allowed_channels.put(_channel())
    aworld.plan(JIRA)
    aworld.world.connectors.issues = [ISSUE]

    aworld.ask("what is WEAVES-1?")

    assert _public(aworld) == []
    (whisper,) = aworld.slack.ephemerals
    assert "From WEAVES-1" in whisper["text"]


def _channel():  # type: ignore[no-untyped-def]
    from contracts.v1.slack_access import SlackAllowedChannel

    now = datetime.now(UTC)
    return SlackAllowedChannel(
        id="local-org:C0CHAN001",
        org_id="local-org",
        channel_id="C0CHAN001",
        added_by="local-developer",
        created_at=now,
        updated_at=now,
    )


def test_a_paused_bot_cancels_the_queued_turn_and_the_asker_is_told_privately(
    aworld: AskWorld,
) -> None:
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.runtime.slack_access_service.set_paused(True)

    turn = aworld.work()
    aworld.reply()

    assert turn.status is TaskTurnStatus.CANCELLED
    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.UNAVAILABLE]
    assert _public(aworld) == []
    assert aworld.latest().reply_state == "posted"


def test_a_failed_turn_gets_a_short_reference_and_no_error_text(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider exploded with a secret detail")

    monkeypatch.setattr(aworld.runtime.agent_execution_service, "run_agent", broken)

    turn = aworld.ask("what does idempotent mean?")

    assert turn.status is TaskTurnStatus.FAILED
    (message,) = _public(aworld)
    assert message["text"] == copy.failure(compose.short_ref(turn.turn_id))
    assert "exploded" not in message["text"] and "secret" not in message["text"]
    assert aworld.latest().run_state == "failed"


# -- scene G: the working message ----------------------------------------------------


def test_a_queued_request_shows_a_notice_only_after_ten_seconds(
    aworld: AskWorld,
) -> None:
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    start = aworld.latest().requested_at

    aworld.at(start + timedelta(seconds=5))
    aworld.reply()
    assert aworld.slack.calls == []  # the eyes reaction was enough

    aworld.at(start + timedelta(seconds=11))
    aworld.reply()
    assert aworld.slack.texts() == [copy.QUEUED]


def test_one_working_message_is_edited_in_place_through_the_real_steps(
    aworld: AskWorld,
) -> None:
    aworld.open_audience()
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    start = aworld.latest().requested_at
    aworld.at(start + timedelta(seconds=3))

    aworld.force_running()  # the guard has not finished: "Checking sources"
    aworld.reply()
    assert aworld.slack.texts() == [copy.CHECKING]

    from contracts.v1 import TurnVerdict

    verdict = TurnVerdict(
        outcome="allowed",
        tools_green=(JIRA,),
        tools_needed=(JIRA,),
        planner_status="valid",
    )
    aworld.force_running(verdict)  # approved to read Jira, not yet read
    aworld.reply()
    assert aworld.slack.texts() == [copy.checking("Jira")]

    aworld.at(start + timedelta(minutes=2, seconds=5))
    aworld.reply()
    assert aworld.slack.texts() == [copy.still_working(2)]

    aworld.at(start + timedelta(minutes=5, seconds=5))
    aworld.reply()
    assert aworld.slack.texts() == [copy.TAKING_LONGER]

    assert aworld.slack.count("chat.postMessage") == 1  # edited, never re-posted
    assert aworld.slack.count("chat.update") == 3


def test_the_working_message_becomes_the_receipt_and_the_answer_is_a_new_post(
    aworld: AskWorld,
) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    start = aworld.latest().requested_at
    aworld.at(start + timedelta(seconds=12))
    aworld.reply()  # queued notice is up
    assert aworld.slack.texts() == [copy.QUEUED]

    aworld.work()
    aworld.at(start + timedelta(seconds=41))
    aworld.reply()

    status, answer = _public(aworld)
    assert status["text"].startswith(":white_check_mark: Done in ")
    assert "From general knowledge" in answer["text"]
    row = aworld.latest()
    assert row.posts == 2 and row.reply_state == "posted"
    assert aworld.slack.count("chat.postMessage") == 2  # never more than two posts


def test_a_private_reply_removes_the_working_message(aworld: AskWorld) -> None:
    assert aworld.world.jira is not None
    aworld.world.snapshot(aworld.world.jira, "healthy")
    aworld.plan(JIRA)
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    start = aworld.latest().requested_at
    aworld.at(start + timedelta(seconds=12))
    aworld.reply()
    assert len(_public(aworld)) == 1  # the queued notice

    aworld.work()
    aworld.reply()

    assert _public(aworld) == []  # deleted
    assert aworld.slack.ephemerals[0]["text"].startswith(":lock: Jira isn't open")


def test_a_request_past_the_deadline_fails_with_a_reference(aworld: AskWorld) -> None:
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    row = aworld.latest()
    aworld.at(row.requested_at + timedelta(seconds=12))
    aworld.reply()
    aworld.at(row.requested_at + timedelta(minutes=16))

    aworld.reply()

    (status,) = _public(aworld)  # the working message turned into the failure
    assert status["text"] == copy.failure(compose.short_ref(row.turn_id))
    assert (
        aworld.latest().run_state == "failed"
        and aworld.slack.count("chat.postMessage") == 1
    )


def test_native_status_is_used_instead_of_a_message_when_the_flag_is_on(
    aworld: AskWorld,
) -> None:
    aworld.runtime.slack_reply_tick._native = lambda: True
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.force_running()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=3))

    aworld.reply()

    assert aworld.slack.count("chat.postMessage") == 0
    status = [
        c for c in aworld.slack.calls if c["method"] == "assistant.threads.setStatus"
    ]
    assert status[0]["params"]["status"] == "Checking sources…"
    assert aworld.latest().status_ts == "native"


def test_native_status_falls_back_to_the_message_when_unsupported(
    aworld: AskWorld,
) -> None:
    aworld.slack.status_supported = False
    aworld.runtime.slack_reply_tick._native = lambda: True
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.force_running()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=3))

    aworld.reply()

    assert aworld.slack.texts() == [copy.CHECKING]
    assert aworld.latest().status_ts not in (None, "native")


def test_the_native_status_is_off_by_default(aworld: AskWorld) -> None:
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.force_running()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=3))

    aworld.reply()

    assert aworld.slack.count("assistant.threads.setStatus") == 0


def test_status_describes_the_running_request_privately(aworld: AskWorld) -> None:
    first = aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.force_running()
    aworld.mention("status", thread_ts=first.message_ts)

    aworld.dispatch()

    (whisper,) = aworld.slack.ephemerals
    assert whisper["text"].startswith("Working on your last question: ")
    assert "so far" in whisper["text"]
    assert aworld.slack.messages == []
