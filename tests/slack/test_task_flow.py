"""Scene C: ``@Weaves task: <text>`` creates a normal task as the mapped member."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from contracts.v1 import AgentStatus, TaskAudience, TaskStatus
from modules.slack import copy
from modules.slack.access import messages
from tests.support.slack_ask_world import BOT_TOKEN, AskWorld
from tests.support.slack_pipeline_world import ORG, TEAM, USER
from tests.support.slack_task_world import (
    APP_URL,
    MEMBER,
    TASK_TEXT,
    enable_tasks,
    run_task,
    task_rows,
    tasks,
)

# Built at runtime so no source line looks like a real credential to a scanner.
SLACK_TOKEN = "xo" + "xb-1234567890-abcdefghijklmnop"
TITLE = "prepare the weekly KPI summary"
CREATED = (
    f":white_check_mark: Task created: {TITLE} · Agent: Ops assistant · Running · "
    "I'll post the result here"
)


@pytest.fixture(autouse=True)
def _app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", APP_URL)


def _button(message: dict[str, Any]) -> tuple[str, str] | None:
    blocks = json.loads(message.get("blocks") or "[]")
    for block in blocks:
        for element in block.get("elements", []):
            return element["text"]["text"], element["url"]
    return None


def _decisions(w: AskWorld, action: str) -> list[Any]:
    return [e for e in w.runtime.audit_events.list_scoped(ORG) if e.action == action]


# -- creation -------------------------------------------------------------------------


def test_an_allowed_task_is_a_normal_task_created_as_the_mapped_member(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    event = aworld.mention(f"task: {TASK_TEXT}")

    assert aworld.dispatch() == 1

    (task,) = tasks(aworld)
    assert task.audience is TaskAudience.WORKSPACE  # visible in the dashboard
    assert task.created_by_principal_id == MEMBER == task.requested_by_principal_id
    assert task.title == TITLE and task.status is TaskStatus.OPEN
    assert task.idempotency_key == f"slack:{TEAM}:{event.event_id}"
    assert task.origin is not None and task.origin.kind == "slack"
    turn = aworld.runtime.task_turns.get(task.active_turn_id)
    assert turn.origin is not None and turn.origin.kind == "slack"
    assert (turn.origin.action, turn.origin.event_id) == ("create_task", event.event_id)
    (row,) = task_rows(aworld)
    assert (row.kind, row.tier, row.task_id) == ("task", "execute", task.task_id)
    assert row.requester_principal_id == MEMBER and row.requester_slack_user_id == USER
    (inbox,) = aworld.inbox()
    assert inbox.state == "done" and inbox.text is None
    assert aworld.slack.ephemerals == [] and aworld.slack.messages == []


def test_the_first_message_is_created_then_edited_into_the_receipt(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    aworld.reply()

    (first,) = aworld.slack.messages
    assert first["text"] == CREATED
    (task,) = tasks(aworld)
    assert _button(first) == ("View in Weaves", f"{APP_URL}/#tasks/{task.task_id}")
    assert first["thread_ts"] == aworld.latest().thread_ts

    aworld.work()
    aworld.reply()

    first, result = aworld.slack.messages
    assert first["text"].startswith(f":white_check_mark: Task done: {TITLE} · Ops")
    assert first["text"].count(" · ") == 2  # title · agent · elapsed
    assert _button(first) == ("View in Weaves", f"{APP_URL}/#tasks/{task.task_id}")
    assert result["text"].endswith(copy.TASK_FULL_RESULT)  # a NEW message
    assert _button(result) == ("Open result", f"{APP_URL}/#tasks/{task.task_id}")
    assert result["ts"] != first["ts"]
    row = task_rows(aworld)[0]
    assert (row.reply_state, row.run_state, row.posts) == ("posted", "done", 2)
    assert row.status_ts == first["ts"] and row.answer_ts == result["ts"]
    assert aworld.slack.count("chat.postMessage") == 2
    assert aworld.slack.count("chat.update") == 1


def test_a_second_tick_posts_nothing_more(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    run_task(aworld)
    before = list(aworld.slack.calls)

    for _ in range(3):
        aworld.reply()

    assert aworld.slack.calls == before


def test_a_repeat_task_in_the_same_thread_is_already_tracked(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    (task,) = tasks(aworld)
    thread = aworld.latest().thread_ts

    aworld.mention("task: do something else entirely", thread_ts=thread)
    aworld.dispatch()

    assert len(tasks(aworld)) == 1 and len(task_rows(aworld)) == 1
    tracked = aworld.slack.messages[-1]
    assert tracked["text"] == f"Already tracked in this thread: {TITLE}"
    assert _button(tracked) == ("View task", f"{APP_URL}/#tasks/{task.task_id}")
    assert tracked["thread_ts"] == thread
    assert aworld.slack.ephemerals == []


def test_a_repeat_task_after_the_task_finished_is_still_already_tracked(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    run_task(aworld)
    thread = aworld.latest().thread_ts

    aworld.mention("task: again please", thread_ts=thread)
    aworld.dispatch()

    assert len(tasks(aworld)) == 1
    assert aworld.slack.messages[-1]["text"].startswith("Already tracked in this thr")


def test_an_empty_task_asks_for_the_text_and_creates_nothing(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    aworld.mention("task:")
    aworld.dispatch()

    assert tasks(aworld) == [] and task_rows(aworld) == []
    assert aworld.slack.messages[-1]["text"] == copy.MISSING_TASK_TEXT
    assert _decisions(aworld, "slack.execute.allowed") == []  # nothing is counted


def test_without_a_default_agent_the_asker_is_told_and_nothing_is_created(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld, agent=False)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    assert tasks(aworld) == []
    assert [e["text"] for e in aworld.slack.ephemerals] == [copy.NO_TASK_AGENT]
    assert (
        copy.NO_TASK_AGENT == "No agent is set up for Slack tasks. Ask a Weaves admin."
    )


def test_a_default_agent_that_was_archived_counts_as_unset(aworld: AskWorld) -> None:
    agent_id = enable_tasks(aworld)
    agent = aworld.runtime.agents.get(agent_id)
    aworld.runtime.agents.put(
        agent.model_copy(
            update={"status": AgentStatus.ARCHIVED, "current_version_id": None}
        )
    )
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    assert tasks(aworld) == []
    assert aworld.slack.ephemerals[0]["text"] == copy.NO_TASK_AGENT


def test_a_blocked_user_gets_scene_e_privately_and_the_attempt_is_recorded(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld, rule="off")
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    assert tasks(aworld) == [] and aworld.slack.messages == []
    assert aworld.slack.ephemerals[0]["text"] == (
        ":lock: I'm not set up to create tasks here. Ask a Weaves admin."
    )
    (blocked,) = _decisions(aworld, "slack.execute.blocked")
    assert blocked.metadata["reason"] == "rule_off"
    assert blocked.metadata["slack_user_id"] == USER
    assert _decisions(aworld, "slack.execute.allowed") == []
    assert aworld.world.models.planner_calls == 0


# -- one task per event, replays and restarts ------------------------------------------


def test_the_same_event_dispatched_twice_makes_one_task_and_one_row(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    event = aworld.mention(f"task: {TASK_TEXT}")
    dispatcher = aworld.runtime.slack_dispatcher

    dispatcher._handle(event)
    dispatcher._handle(event)  # a replay after a crash before the row was settled

    assert len(tasks(aworld)) == 1 and len(task_rows(aworld)) == 1
    assert len(aworld.runtime.task_turns.list()) == 1
    assert len(_decisions(aworld, "slack.execute.allowed")) == 1  # counted once


def test_a_crash_between_creating_the_task_and_the_row_is_finished_by_the_replay(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_tasks(aworld)
    event = aworld.mention(f"task: {TASK_TEXT}")
    flow = aworld.runtime.slack_dispatcher._task
    original = flow._book.start

    def crash(row: Any) -> Any:
        raise RuntimeError("process died")

    monkeypatch.setattr(flow._book, "start", crash)
    with pytest.raises(RuntimeError):
        aworld.runtime.slack_dispatcher._handle(event)
    monkeypatch.setattr(flow._book, "start", original)

    # The task creation was rolled back or is replayed by its key: one task either way.
    aworld.runtime.slack_dispatcher._handle(event)

    assert len(tasks(aworld)) == 1 and len(task_rows(aworld)) == 1


def test_a_lost_result_edit_is_retried_without_posting_the_result_twice(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    aworld.reply()
    aworld.work()
    aworld.slack.faults["chat.update"] = ["500"]

    aworld.reply()  # result posted, receipt edit failed

    assert aworld.slack.count("chat.postMessage") == 2
    row = task_rows(aworld)[0]
    assert row.answer_ts is not None and row.reply_state == "pending"
    aworld.at(datetime.now(UTC) + timedelta(minutes=5))

    aworld.reply()

    assert aworld.slack.count("chat.postMessage") == 2  # still two posts
    assert task_rows(aworld)[0].reply_state == "posted"
    assert aworld.slack.messages[0]["text"].startswith(":white_check_mark: Task done")


def test_a_restart_after_the_created_post_does_not_post_it_again(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    aworld.reply()
    aworld.work()
    # A fresh process: the reply tick is rebuilt from the stored rows only.
    aworld.runtime.slack_reply_tick._failures.clear()

    aworld.reply()
    aworld.reply()

    assert aworld.slack.count("chat.postMessage") == 2
    assert aworld.slack.messages[0]["text"].startswith(":white_check_mark: Task done")


# -- failure and refusal --------------------------------------------------------------


def test_a_failed_task_edits_the_first_message_and_posts_nothing_else(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    aworld.reply()
    (task,) = tasks(aworld)

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("model blew up: SECRET-DETAIL-9917")

    monkeypatch.setattr(aworld.runtime.model_gateway, "complete", explode)
    turn = aworld.work()
    aworld.reply()

    assert turn.status.value == "failed"
    (only,) = aworld.slack.messages
    assert only["text"].startswith(
        f":warning: I couldn't finish this task: {TITLE} (ref "
    )
    assert "SECRET-DETAIL-9917" not in json.dumps(aworld.slack.messages)
    row = task_rows(aworld)[0]
    assert (row.run_state, row.reply_state, row.posts) == ("failed", "posted", 1)
    assert task.task_id in (_button(only) or ("", ""))[1]


def test_a_member_removed_before_pickup_cancels_the_task_with_a_private_cause(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    aworld.reply()
    aworld.runtime.slack_access_service.allowlists.members.is_active = lambda _: False

    turn = aworld.work()
    aworld.reply()

    assert turn.status.value == "cancelled"
    assert turn.origin.verdict.cancel_reason == "slack_access_revoked"
    (only,) = aworld.slack.messages
    assert messages.NOT_MAPPED in only["text"]
    assert task_rows(aworld)[0].reply_state == "posted"
    assert not aworld.runtime.agent_runs.list()  # the model never ran


def test_a_task_waiting_for_a_slot_posts_only_the_created_message(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    for _ in range(3):
        aworld.reply()

    (first,) = aworld.slack.messages
    assert first["text"] == CREATED
    assert aworld.slack.count("chat.update") == 0
    assert task_rows(aworld)[0].reply_state == "pending"


# -- the result message ---------------------------------------------------------------


def test_a_long_result_is_cut_to_a_summary_a_few_bullets_and_the_link(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_tasks(aworld)
    long_result = (
        "Weekly KPI summary for the leadership meeting.\n\n"
        + "\n".join(f"- Metric {i}: value {i} " + "x" * 300 for i in range(12))
        + "\n\n"
        + "More prose. " * 400
    )
    from modules.slack.tasks import reply

    monkeypatch.setattr(
        reply.TaskReply,
        "_result_body",
        lambda self, turn: reply.result_text(long_result),
    )
    run_task(aworld)

    result = aworld.slack.messages[1]["text"]
    assert result.startswith("Weekly KPI summary for the leadership meeting.")
    assert result.count("\n- ") == 5 or result.count("• ") == 5
    assert len(result) < 2900 and result.endswith(copy.TASK_FULL_RESULT)
    assert "Metric 5" not in result


def test_the_result_is_sanitized_and_scrubbed(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_tasks(aworld)
    nasty = (
        "Done, <!channel> please read <@U0VICTIM> and mail bob@example.com.\n"
        f"- token {SLACK_TOKEN} leaked\n"
        "- see https://evil.example.com/steal"
    )
    from modules.slack.tasks import reply

    monkeypatch.setattr(
        reply.TaskReply, "_result_body", lambda self, turn: reply.result_text(nasty)
    )
    run_task(aworld)

    result = aworld.slack.messages[1]["text"]
    assert "<!channel>" not in result and "<@U0VICTIM>" not in result
    assert "bob@example.com" not in result and SLACK_TOKEN not in result
    assert "https://evil.example.com" not in result


def test_a_title_with_mentions_cannot_notify_anyone(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    aworld.mention("task: <!channel> ping <@U0VICTIM> about *the* launch")
    aworld.dispatch()
    aworld.reply()

    text = aworld.slack.messages[0]["text"]
    assert "<!channel>" not in text and "<@U0VICTIM>" not in text


# -- no leakage ------------------------------------------------------------------------


def test_nothing_sensitive_reaches_logs_audit_or_thread_rows(
    aworld: AskWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    enable_tasks(aworld)
    marker = "ZEBRA-TASK-TEXT-5521"
    run_task(aworld, f"summarize {marker}")

    haystack = "\n".join(r.getMessage() for r in caplog.records)
    haystack += json.dumps(
        [
            json.loads(e.model_dump_json())
            for e in aworld.runtime.audit_events.list_scoped(ORG)
        ]
    )
    haystack += json.dumps(
        [json.loads(r.model_dump_json()) for r in aworld.runtime.slack_threads.list()]
    )
    assert marker not in haystack
    assert BOT_TOKEN not in haystack
    for event in _decisions(aworld, "slack.decision"):
        assert marker not in str(event.metadata)
