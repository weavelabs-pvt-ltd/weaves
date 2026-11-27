"""``task:`` access: the rule matrix, Execute limits, connectors and the task API."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from contracts.v1.slack_access import SlackSettingsUpdate
from modules.slack import copy
from modules.slack.access import messages
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import ORG, USER
from tests.support.slack_task_world import (
    APP_URL,
    TASK_TEXT,
    allow_user,
    enable_tasks,
    make_agent,
    task_rows,
    tasks,
)

RULES = ["off", "user_only", "channel_only", "user_and_channel", "user_or_channel"]
NOT_SET_UP = f":lock: {copy.NOT_SET_UP['task']}"


@pytest.fixture(autouse=True)
def _app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", APP_URL)


def _expected(rule: str, user: bool, channel: bool, mapped: bool) -> str | None:
    """The refusal text of the design's rule matrix, or ``None`` when allowed.

    Written from design 3.5 (not from the engine): the rule first, then the hard
    check that the listed user is mapped to an active member.
    """
    if rule == "off":
        return NOT_SET_UP
    if rule == "user_only" and not user:
        return f":lock: {messages.USER_NOT_LISTED}"
    if rule == "channel_only" and not channel:
        return f":lock: {messages.CHANNEL_NOT_LISTED}"
    if rule == "user_and_channel":
        if not user:
            return f":lock: {messages.USER_NOT_LISTED}"
        if not channel:
            return f":lock: {messages.CHANNEL_NOT_LISTED}"
    if rule == "user_or_channel" and not (user or channel):
        return f":lock: {messages.USER_NOT_LISTED}"
    if not (user and mapped):  # nothing maps this Slack id to a Weaves member
        return f":lock: {messages.NOT_MAPPED}"
    return None


@pytest.mark.parametrize(
    ("rule", "user", "channel", "mapped"),
    list(itertools.product(RULES, (True, False), (True, False), (True, False))),
)
def test_the_rule_matrix(
    aworld: AskWorld, rule: str, user: bool, channel: bool, mapped: bool
) -> None:
    enable_tasks(aworld, rule=rule, listed=user, mapped=mapped, channel_listed=channel)
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    refusal = _expected(rule, user, channel, mapped)
    if refusal is None:
        assert len(tasks(aworld)) == 1 and len(task_rows(aworld)) == 1
        assert aworld.slack.ephemerals == []
    else:
        assert tasks(aworld) == [] and task_rows(aworld) == []
        assert [e["text"] for e in aworld.slack.ephemerals] == [refusal]
        assert aworld.slack.messages == []  # a refusal is never public
        assert aworld.world.models.planner_calls == 0


def test_an_inactive_member_never_executes(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    aworld.runtime.slack_access_service.allowlists.members.is_active = lambda _: False

    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    assert tasks(aworld) == []
    assert aworld.slack.ephemerals[0]["text"] == f":lock: {messages.NOT_MAPPED}"


def test_a_member_without_the_permission_never_executes(aworld: AskWorld) -> None:
    enable_tasks(aworld)
    members = aworld.runtime.slack_access_service.allowlists.members
    members.permissions = lambda _: frozenset({"tasks.create"})  # no agents.run

    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()

    assert tasks(aworld) == []
    assert aworld.slack.ephemerals[0]["text"] == f":lock: {messages.NO_PERMISSION}"


# -- Execute limits --------------------------------------------------------------------


def _mention_in_new_thread(w: AskWorld, text: str = TASK_TEXT, **kw: Any) -> None:
    w.mention(f"task: {text}", thread_ts=f"17000{w.serial + 1:05d}.000100", **kw)


def test_execute_limits_default_to_ten_per_user_and_thirty_per_channel(
    aworld: AskWorld,
) -> None:
    view = aworld.runtime.slack_access_service.view()
    assert view["execute_limits"] == {"user_per_10_min": 10, "channel_per_10_min": 30}
    assert view["default_task_agent_id"] is None


def test_the_per_user_limit_refuses_the_next_task_privately(aworld: AskWorld) -> None:
    enable_tasks(
        aworld, execute_limits={"user_per_10_min": 2, "channel_per_10_min": 30}
    )
    for _ in range(3):
        _mention_in_new_thread(aworld)
    aworld.dispatch()

    assert len(tasks(aworld)) == 2
    (refusal,) = [e["text"] for e in aworld.slack.ephemerals]
    assert refusal.startswith(
        ":hourglass_flowing_sand: You're asking a lot; try again in"
    )
    assert len(task_rows(aworld)) == 2
    blocked = [
        e
        for e in aworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.execute.blocked"
    ]
    assert [b.metadata["reason"] for b in blocked] == ["rate_limit_user"]


def test_the_per_channel_limit_counts_every_user_in_the_channel(
    aworld: AskWorld,
) -> None:
    enable_tasks(
        aworld, execute_limits={"user_per_10_min": 10, "channel_per_10_min": 2}
    )
    allow_user(aworld, user="U0OTHER02")
    for user in (USER, "U0OTHER02", USER):
        _mention_in_new_thread(aworld, user=user)
    aworld.dispatch()

    assert len(tasks(aworld)) == 2
    assert len(aworld.slack.ephemerals) == 1
    assert "try again in" in aworld.slack.ephemerals[0]["text"]
    # Another channel is not affected.
    _mention_in_new_thread(aworld, channel="C0OTHERCH")
    aworld.dispatch()
    assert len(tasks(aworld)) == 3


def test_the_window_rolls_after_ten_minutes(aworld: AskWorld) -> None:
    enable_tasks(
        aworld, execute_limits={"user_per_10_min": 1, "channel_per_10_min": 30}
    )
    _mention_in_new_thread(aworld)
    aworld.dispatch()
    _mention_in_new_thread(aworld)
    aworld.dispatch()
    assert len(tasks(aworld)) == 1

    later = datetime.now(UTC) + timedelta(minutes=11)
    aworld.runtime.slack_dispatcher._clock = lambda: later
    _mention_in_new_thread(aworld)
    aworld.dispatch()

    assert len(tasks(aworld)) == 2


def test_refusals_empty_requests_and_replays_do_not_count(aworld: AskWorld) -> None:
    enable_tasks(
        aworld, execute_limits={"user_per_10_min": 2, "channel_per_10_min": 30}
    )
    aworld.mention("task:")  # asks for the text: not counted
    aworld.dispatch()
    event = aworld.mention(f"task: {TASK_TEXT}", thread_ts="1700000001.000100")
    aworld.dispatch()
    aworld.runtime.slack_dispatcher._handle(event)  # a replay: not counted again
    _mention_in_new_thread(aworld)
    aworld.dispatch()

    assert len(tasks(aworld)) == 2  # both were within the limit of two
    assert aworld.slack.ephemerals == []


def test_ask_and_execute_have_separate_budgets(aworld: AskWorld) -> None:
    enable_tasks(
        aworld, execute_limits={"user_per_10_min": 1, "channel_per_10_min": 30}
    )
    aworld.ask("what does idempotent mean?")
    _mention_in_new_thread(aworld)
    aworld.dispatch()

    assert len(task_rows(aworld)) == 1  # the Ask did not use the Execute budget


def test_the_pickup_guard_does_not_count_the_request_against_itself(
    aworld: AskWorld,
) -> None:
    enable_tasks(aworld, execute_limits={"user_per_10_min": 1, "channel_per_10_min": 1})
    aworld.mention(f"task: {TASK_TEXT}")
    aworld.dispatch()
    turn = aworld.work()

    assert turn.status.value == "succeeded"
    assert turn.origin.verdict.outcome == "allowed"
    allowed = [
        e
        for e in aworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.execute.allowed"
    ]
    assert len(allowed) == 1  # dispatcher and guard wrote the same record


# -- settings --------------------------------------------------------------------------


def _update(w: AskWorld, **changes: Any) -> dict[str, Any]:
    service = w.runtime.slack_access_service
    current = service.view()
    body = {k: v for k, v in current.items() if k in _EDITABLE}
    body.update(changes)
    return service.update(
        SlackSettingsUpdate(expected_version=current["settings_version"], **body),
        current["settings_version"],
    )


_EDITABLE = {
    "ask_enabled",
    "connector_audience",
    "partial_answers",
    "planner_profile_id",
    "ask_limits",
    "rules",
    "workflow_ids",
    "pr_review_repos",
    "default_task_agent_id",
    "execute_limits",
}


def test_the_default_task_agent_is_validated_against_the_organizations_agents(
    aworld: AskWorld,
) -> None:
    agent_id = make_agent(aworld)

    saved = _update(aworld, default_task_agent_id=agent_id)
    assert saved["default_task_agent_id"] == agent_id
    with pytest.raises(ValueError, match="not an agent"):
        _update(aworld, default_task_agent_id="no-such-agent")
    cleared = _update(aworld, default_task_agent_id=None)
    assert cleared["default_task_agent_id"] is None


def test_an_older_client_that_omits_the_new_fields_keeps_them(
    aworld: AskWorld,
) -> None:
    agent_id = make_agent(aworld)
    _update(
        aworld,
        default_task_agent_id=agent_id,
        execute_limits={"user_per_10_min": 7, "channel_per_10_min": 9},
    )
    service = aworld.runtime.slack_access_service
    current = service.view()
    old_body = {
        k: v
        for k, v in current.items()
        if k in _EDITABLE - {"default_task_agent_id", "execute_limits"}
    }

    saved = service.update(
        SlackSettingsUpdate(expected_version=current["settings_version"], **old_body),
        current["settings_version"],
    )

    assert saved["default_task_agent_id"] == agent_id
    assert saved["execute_limits"] == {"user_per_10_min": 7, "channel_per_10_min": 9}


def test_the_task_agent_and_limits_changes_are_audited(aworld: AskWorld) -> None:
    agent_id = make_agent(aworld)
    _update(aworld, default_task_agent_id=agent_id)

    (event,) = [
        e
        for e in aworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.access.settings_updated"
    ]
    assert event.metadata["default_task_agent_id"] == agent_id
    assert "execute_limits" in event.metadata
