"""The dispatcher: a mention becomes a hidden Q&A turn, a cheap reply, or nothing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1 import (
    ModelProfileStatus,
    OrganizationLifecycleState,
    Permission,
    PrincipalType,
    TaskAudience,
)
from contracts.v1.slack_access import AskLimits, SlackAllowedUser
from modules.slack import copy
from modules.slack.access import messages
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import CHANNEL, ORG, TEAM, USER


def _decisions(w: AskWorld) -> list:
    return [
        e
        for e in w.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.decision"
    ]


def _tasks(w: AskWorld) -> list:
    return list(w.runtime.tasks.list())


def test_an_ask_becomes_one_hidden_private_task_of_the_service_principal(
    aworld: AskWorld,
) -> None:
    event = aworld.mention("what is the status of WEAVES-1?")

    assert aworld.dispatch() == 1

    (task,) = _tasks(aworld)
    assert task.audience is TaskAudience.PRIVATE
    assert task.idempotency_key == f"slack:{TEAM}:{event.event_id}"
    principal = aworld.runtime.principals.get(task.created_by_principal_id)
    assert principal.principal_type is PrincipalType.SERVICE_ACCOUNT
    # Exactly agents.run and tasks.create, nothing else.
    (binding,) = [
        b
        for b in aworld.runtime.role_bindings.list_scoped(ORG)
        if b.principal_id == principal.id
    ]
    role = aworld.runtime.roles.get(binding.role_id)
    assert set(role.permissions) == {Permission.AGENTS_RUN, Permission.TASKS_CREATE}
    turn = aworld.runtime.task_turns.get(task.active_turn_id)
    origin = turn.origin
    assert origin is not None and origin.kind == "slack_qa" and origin.action == "ask"
    assert (origin.team_id, origin.channel_id, origin.slack_user_id) == (
        TEAM,
        CHANNEL,
        USER,
    )
    assert origin.event_id == event.event_id and origin.thread_ts == event.message_ts
    assert turn.request_id == f"slack-{event.event_id}"
    (row,) = aworld.threads()
    assert (row.task_id, row.turn_id) == (task.task_id, turn.turn_id)
    assert row.reply_state == "pending" and row.request_count == 1
    (inbox,) = aworld.inbox()
    assert inbox.state == "done" and inbox.text is None  # the text is not kept


def test_the_service_principal_and_agent_are_created_once(aworld: AskWorld) -> None:
    version_before = aworld.runtime.slack_access_service.view()["settings_version"]
    aworld.mention("first")
    aworld.dispatch()
    aworld.mention("second")  # a different thread
    aworld.dispatch()

    assert len({t.created_by_principal_id for t in _tasks(aworld)}) == 1
    assert len({t.agent_id for t in _tasks(aworld)}) == 1
    principal_id, version_id = aworld.runtime.slack_access_service.qa_ids()
    assert principal_id and version_id
    # Provisioning is not a settings change: the page version does not move.
    after = aworld.runtime.slack_access_service.view()["settings_version"]
    assert after == max(version_before, 1)


def test_the_agent_lists_every_read_capability_and_follows_new_connectors(
    aworld: AskWorld,
) -> None:
    aworld.mention("first")
    aworld.dispatch()
    (task,) = _tasks(aworld)
    version = aworld.runtime.agent_versions.get(task.agent_version_id)
    assert set(version.allowed_capability_ids) == {
        "jira.issues.search",
        "slack.messages.search",
    }
    aworld.world.disable(aworld.world.slack)  # a connector goes away
    aworld.mention("second")
    aworld.dispatch()

    newest = max(_tasks(aworld), key=lambda t: t.created_at)
    latest = aworld.runtime.agent_versions.get(newest.agent_version_id)
    assert latest.version == 2
    assert latest.allowed_capability_ids == ("jira.issues.search",)


def test_a_replayed_event_creates_nothing_more(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    aworld.mention("what is WEAVES-1?")
    store = aworld.runtime.slack_store
    real = store.settle_event
    crashed = {"done": False}

    def crash_once(*args, **kwargs):
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("worker died before the inbox row was settled")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "settle_event", crash_once)
    assert aworld.dispatch() == 0
    assert aworld.inbox("pending")  # still pending: it will be replayed

    assert aworld.dispatch() == 1

    assert len(_tasks(aworld)) == 1 and len(aworld.threads()) == 1
    assert len(aworld.runtime.task_turns.list()) == 1


def test_concurrent_dispatchers_make_one_task_per_event(aworld: AskWorld) -> None:
    event = aworld.mention("what is WEAVES-1?")
    dispatcher = aworld.runtime.slack_dispatcher

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: dispatcher._handle(event), range(6)))

    assert len(_tasks(aworld)) == 1 and len(aworld.threads()) == 1
    assert len(aworld.runtime.task_turns.list()) == 1
    (inbox,) = aworld.inbox()
    assert inbox.state == "done"
    # Nobody was told anything: one Ask, no refusals.
    assert aworld.slack.count("chat.postEphemeral") == 0


def test_a_second_mention_in_the_thread_continues_the_same_conversation(
    aworld: AskWorld,
) -> None:
    first = aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.work()
    aworld.reply()
    aworld.mention("and what about WEAVES-2?", thread_ts=first.message_ts)

    aworld.dispatch()

    (task,) = _tasks(aworld)  # still one hidden conversation
    (row,) = aworld.threads()
    assert row.request_count == 2 and row.reply_state == "pending"
    assert len(aworld.runtime.task_turns.list()) == 2
    assert [m.content for m in aworld.runtime.task_messages.list()][:1] == [
        "what is WEAVES-1?"
    ]
    assert task.task_id == row.task_id


def test_a_mention_while_the_last_answer_is_going_out_waits_then_gives_up(
    aworld: AskWorld,
) -> None:
    first = aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.mention("and WEAVES-2?", thread_ts=first.message_ts)

    assert aworld.dispatch() == 0  # first still pending: wait, do not answer
    assert aworld.inbox("pending")
    assert aworld.slack.count("chat.postEphemeral") == 0

    old = datetime.now(UTC) - timedelta(minutes=5)
    aworld.runtime.slack_store._events = {  # age the waiting mention past patience
        k: (v if v.event_id == first.event_id else _aged(v, old))
        for k, v in aworld.runtime.slack_store._events.items()
    }
    assert aworld.dispatch() == 1
    assert aworld.slack.ephemerals[-1]["text"] == copy.BUSY
    assert len(_tasks(aworld)) == 1 and len(aworld.runtime.task_turns.list()) == 1


def _aged(event, when):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    return replace(event, received_at=when)


# -- cheap exits: no task, no model, no quota ------------------------------------

CHEAP = [
    ("", copy.HELP, False),
    ("help", copy.HELP, False),
    ("hi", copy.GREETING, False),
    ("thanks", copy.THANKS, False),
    ("status", copy.NO_ACTIVITY, True),
    ("stop", copy.COMING_SOON["stop"], True),
    ("retry", copy.COMING_SOON["retry"], True),
    ("review", copy.MISSING_PR, False),
    ("run", copy.MISSING_WORKFLOW, False),
]


@pytest.mark.parametrize(("text", "reply", "private"), CHEAP)
def test_cheap_exits_create_no_task_call_no_model_and_do_not_count(
    aworld: AskWorld, text: str, reply: str, private: bool
) -> None:
    aworld.mention(text)

    assert aworld.dispatch() == 1

    assert _tasks(aworld) == [] and aworld.threads() == []
    assert (aworld.world.models.planner_calls, aworld.world.models.answer_calls) == (
        0,
        0,
    )
    assert _decisions(aworld) == []  # nothing for the Ask limits to count
    assert aworld.world.connectors.total() == 0
    assert aworld.runtime.slack_access_service.qa_ids() == (
        None,
        None,
    )  # not even provisioned
    sent = aworld.slack.ephemerals if private else aworld.slack.messages
    assert [m["text"] for m in sent] == [reply]
    assert len(aworld.slack.calls) == 1


def test_a_user_at_the_ask_limit_can_still_get_help(aworld: AskWorld) -> None:
    aworld.world.set_slack_settings(
        ask_limits=AskLimits(user_per_10_min=1).model_dump()
    )
    aworld.ask("first question")

    aworld.mention("second question")
    aworld.mention("help")
    aworld.dispatch()

    assert copy.HELP in aworld.slack.texts()
    assert any("try again in" in e["text"] for e in aworld.slack.ephemerals)


def test_execute_commands_are_never_run(aworld: AskWorld) -> None:
    aworld.world.set_slack_settings(
        rules={
            "create_task": "user_only",
            "pr_review": "user_only",
            "run_workflow": "user_only",
            "ask_private": "off",
        }
    )
    now = datetime.now(UTC)
    aworld.runtime.slack_allowed_users.put(
        SlackAllowedUser(
            id=f"{ORG}:{USER}",
            org_id=ORG,
            slack_user_id=USER,
            principal_id=None,
            added_by="local-developer",
            created_at=now,
            updated_at=now,
        )
    )
    aworld.mention("task: write the release notes")
    aworld.mention("review https://github.com/acme/api/pull/42")
    aworld.mention("run weekly-report")
    aworld.dispatch()

    assert _tasks(aworld) == [] and aworld.threads() == []
    assert aworld.world.models.planner_calls == 0
    sent = [e["text"] for e in aworld.slack.ephemerals]
    # A listed but unmapped person is refused for task and review (no member), and
    # the workflow command passes its rule and is told it is coming; none ran.
    assert sent[:2] == [f":lock: {messages.NOT_MAPPED}"] * 2
    assert sent[2] == copy.COMING_SOON["run"]


def test_an_execute_command_without_its_rule_gets_the_scene_e_text(
    aworld: AskWorld,
) -> None:
    aworld.mention("task: write the release notes")
    aworld.dispatch()

    assert _tasks(aworld) == []
    assert aworld.slack.ephemerals[0]["text"] == f":lock: {copy.NOT_SET_UP['task']}"


# -- gates at dispatch -------------------------------------------------------------


def test_the_ask_rate_limit_replies_privately_with_the_wait_and_creates_nothing(
    aworld: AskWorld,
) -> None:
    aworld.world.set_slack_settings(
        ask_limits=AskLimits(user_per_10_min=1).model_dump()
    )
    aworld.ask("first question")  # one counted decision at pickup

    aworld.mention("second question")
    aworld.dispatch()

    assert len(_tasks(aworld)) == 1  # only the first
    text = aworld.slack.ephemerals[-1]["text"]
    assert text.startswith(":hourglass_flowing_sand: You're asking a lot; try again in")
    assert "minutes" in text or "minute" in text


def test_a_long_question_is_refused_without_a_task(aworld: AskWorld) -> None:
    aworld.mention("x" * 1200)
    aworld.dispatch()

    assert _tasks(aworld) == []
    assert "too long" in aworld.slack.ephemerals[0]["text"]


def test_a_paused_bot_says_unavailable_once_per_channel_per_hour(
    aworld: AskWorld,
) -> None:
    aworld.runtime.slack_access_service.set_paused(True)
    aworld.mention("one")
    aworld.mention("two")
    aworld.mention("three", channel="C0OTHER01")
    aworld.dispatch()

    assert _tasks(aworld) == []
    texts = [e["text"] for e in aworld.slack.ephemerals]
    assert texts == [messages.UNAVAILABLE, messages.UNAVAILABLE]  # 2 channels, not 3
    assert all(e.state == "done" for e in aworld.inbox())


def test_a_quarantined_org_gets_the_neutral_line_and_no_task(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle = aworld.runtime.organization_lifecycle_service
    monkeypatch.setattr(
        lifecycle, "state_of", lambda org_id: OrganizationLifecycleState.SUSPENDED
    )
    aworld.mention("anything")
    aworld.mention("help")

    aworld.dispatch()

    assert _tasks(aworld) == []
    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.UNAVAILABLE]
    assert aworld.slack.messages == []  # not even the help text leaks out


def test_the_platform_kill_switch_pauses_everything(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = aworld.runtime.platform_controls
    snapshot = controls.snapshot()
    monkeypatch.setattr(
        controls,
        "snapshot",
        lambda: type(snapshot)(**{**vars(snapshot), "admissions_paused": True}),
    )
    aworld.mention("anything")

    aworld.dispatch()

    assert _tasks(aworld) == []
    assert [e["text"] for e in aworld.slack.ephemerals] == [messages.UNAVAILABLE]


def test_disabled_slack_drops_every_mention_silently(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "false")
    aworld.mention("anything")
    aworld.mention("help")

    aworld.dispatch()

    assert aworld.slack.calls == [] and _tasks(aworld) == []
    assert {e.state for e in aworld.inbox()} == {"dropped"}


def test_a_lost_installation_drops_the_mention(aworld: AskWorld) -> None:
    aworld.runtime.slack_store.set_status(
        "slack-inst-1", "uninstalled", datetime.now(UTC)
    )
    aworld.mention("anything")

    aworld.dispatch()

    assert aworld.slack.calls == [] and _tasks(aworld) == []
    assert {e.state for e in aworld.inbox()} == {"dropped"}


def test_no_active_model_profile_says_so_and_creates_nothing(aworld: AskWorld) -> None:
    for profile in aworld.runtime.model_profiles.list():
        aworld.runtime.model_profiles.put(
            profile.model_copy(update={"status": ModelProfileStatus.DISABLED})
        )
    aworld.mention("what is WEAVES-1?")

    aworld.dispatch()

    assert _tasks(aworld) == []
    assert aworld.slack.ephemerals[0]["text"] == copy.NO_MODEL
    assert aworld.runtime.slack_access_service.qa_ids() == (None, None)


def test_ask_off_in_the_settings_is_told_privately(aworld: AskWorld) -> None:
    aworld.world.set_slack_settings(ask_enabled=False)
    aworld.mention("what is WEAVES-1?")

    aworld.dispatch()

    assert _tasks(aworld) == []
    assert aworld.slack.ephemerals[0]["text"] == messages.ASK_DISABLED
