"""The worker's pre-run guard for Slack turns, end to end on the real worker.

A Slack turn is claimed by the real task worker with the real pipeline, fake
connector servers that count requests and a fake model. Settings, pause,
quarantine, uninstall and member standing are changed after the turn was queued.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from contracts.v1 import (
    OrganizationLifecycleState,
    TaskTurnStatus,
    UsageState,
)
from contracts.v1.slack_access import SlackAllowedUser
from modules.slack.access import messages
from tests.support.slack_pipeline_world import (
    CHANNEL,
    JIRA,
    MARKER,
    ORG,
    SLACK,
    USER,
    World,
)


def _plan(world: World, *needs: str, general_ok: bool | None = None) -> None:
    ok = (not needs) if general_ok is None else general_ok
    world.models.reply = lambda _: json.dumps({"needs": list(needs), "general_ok": ok})


def _open_jira(world: World) -> None:
    assert world.jira is not None and world.slack is not None
    world.set_slack_settings(
        connector_audience={
            world.jira.plugin_installation_id: "everyone",
            world.slack.plugin_installation_id: "everyone",
        }
    )
    world.snapshot(world.jira, "healthy")
    world.snapshot(world.slack, "healthy")


def _decisions(world: World) -> list:
    return [
        e
        for e in world.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.decision"
    ]


def _cancellations(world: World) -> list:
    return [
        e
        for e in world.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.turn.cancelled"
    ]


def test_green_needed_tool_runs_only_that_capability(pworld: World) -> None:
    _open_jira(pworld)
    _plan(pworld, JIRA)
    pworld.queued_turn(f"{MARKER} what is WEAVES-1?")

    done = pworld.process()

    assert done.status is TaskTurnStatus.SUCCEEDED
    assert done.origin is not None and done.origin.capability_ids == (JIRA,)
    assert done.origin.verdict is not None
    assert done.origin.verdict.outcome == "allowed"
    # The agent allows both connectors; only the needed one was ever called.
    assert pworld.connectors.count("jira", "data") == 1
    assert pworld.connectors.count("slack") == 0
    assert (pworld.models.planner_calls, pworld.models.answer_calls) == (1, 1)
    assert len(_decisions(pworld)) == 1
    key = f"task:{done.turn_id}:run:{done.attempts}:plan"
    row = pworld.runtime.usage_service.ledger.get(ORG, key)
    assert row is not None and row.state is UsageState.SETTLED


def test_model_only_plan_runs_no_connector(pworld: World) -> None:
    _open_jira(pworld)
    _plan(pworld)
    pworld.queued_turn(f"{MARKER} hello there")

    done = pworld.process()

    assert done.status is TaskTurnStatus.SUCCEEDED
    assert done.origin is not None and done.origin.capability_ids == ()
    assert done.origin.verdict is not None
    assert done.origin.verdict.outcome == "model_only"
    assert pworld.connectors.total() == 0
    assert (pworld.models.planner_calls, pworld.models.answer_calls) == (1, 1)


def test_red_tool_finishes_with_a_fixed_message_and_no_answer_model(
    pworld: World,
) -> None:
    # Jira keeps its default audience (not in Slack): the plan needs it, access is red.
    assert pworld.jira is not None
    pworld.snapshot(pworld.jira, "healthy")
    pworld.snapshot(pworld.slack, "healthy")
    _plan(pworld, JIRA)
    pworld.queued_turn(f"{MARKER} what is WEAVES-1?")

    done = pworld.process()

    assert done.status is TaskTurnStatus.SUCCEEDED and done.agent_run_id is None
    reply = pworld.runtime.task_messages.get(done.assistant_message_id)
    assert reply.content == messages.tool_red(
        "Jira",
        pytest.importorskip("modules.slack.access.models").Reason.AUDIENCE_NOT_IN_SLACK,
    )
    assert done.origin is not None and done.origin.verdict is not None
    assert done.origin.verdict.outcome == "refused_access"
    assert done.origin.verdict.answer_ephemeral is True
    assert done.origin.capability_ids == ()
    assert pworld.connectors.total() == 0
    assert pworld.models.answer_calls == 0


def test_a_failed_planner_fails_closed_with_try_again(pworld: World) -> None:
    _open_jira(pworld)
    pworld.models.reply = lambda _: "not json"
    pworld.queued_turn(f"{MARKER} what is going on?")

    done = pworld.process()

    reply = pworld.runtime.task_messages.get(done.assistant_message_id)
    assert reply.content == messages.PLANNER_FAILED
    assert done.origin is not None and done.origin.verdict is not None
    assert done.origin.verdict.outcome == "planner_failed"
    assert pworld.connectors.total() == 0 and pworld.models.answer_calls == 0


def _cancelled(world: World) -> tuple:
    done = world.process()
    assert done.status is TaskTurnStatus.CANCELLED
    assert done.origin is not None and done.origin.verdict is not None
    assert done.origin.verdict.cancel_reason == "slack_access_revoked"
    assert done.origin.capability_ids == ()
    # Nothing looked up, no model of any kind.
    assert world.connectors.total() == 0
    assert (world.models.planner_calls, world.models.answer_calls) == (0, 0)
    audit = _cancellations(world)
    assert len(audit) == 1 and audit[0].target_id == done.turn_id
    assert MARKER not in json.dumps([e.model_dump(mode="json") for e in audit])
    return done, audit[0]


def test_settings_changed_after_queueing_cancels_the_turn(pworld: World) -> None:
    _open_jira(pworld)
    pworld.queued_turn(f"{MARKER} anything")
    pworld.set_slack_settings(ask_enabled=False)

    _, event = _cancelled(pworld)

    assert event.metadata["cause"] == "ask_disabled"


def test_pausing_the_bot_cancels_a_queued_turn(pworld: World) -> None:
    _open_jira(pworld)
    pworld.queued_turn(f"{MARKER} anything")
    pworld.runtime.slack_access_service.set_paused(True)

    _, event = _cancelled(pworld)

    assert event.metadata["cause"] == "bot_paused"


def test_a_suspended_or_quarantined_org_cancels_the_turn(
    pworld: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_jira(pworld)
    pworld.queued_turn(f"{MARKER} anything")
    lifecycle = pworld.runtime.organization_lifecycle_service
    monkeypatch.setattr(
        lifecycle, "state_of", lambda org_id: OrganizationLifecycleState.SUSPENDED
    )
    pworld.runtime.task_worker_service.turn_guard._org_state = lifecycle.state_of

    _, event = _cancelled(pworld)

    assert event.metadata["cause"] == "org_unavailable"


def test_an_uninstalled_workspace_cancels_the_turn(pworld: World) -> None:
    _open_jira(pworld)
    pworld.queued_turn(f"{MARKER} anything")
    pworld.runtime.slack_store.set_status(
        "slack-inst-1", "uninstalled", datetime.now(UTC)
    )

    _, event = _cancelled(pworld)

    assert event.metadata["cause"] == "installation_inactive"


def test_the_kill_switch_cancels_the_turn(
    pworld: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_jira(pworld)
    pworld.queued_turn(f"{MARKER} anything")
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "false")

    _, event = _cancelled(pworld)

    assert event.metadata["cause"] == "slack_disabled"


def test_a_member_deactivated_after_dispatch_cannot_execute(pworld: World) -> None:
    now = datetime.now(UTC)
    pworld.runtime.slack_allowed_users.put(
        SlackAllowedUser(
            id=f"{ORG}:{USER}",
            org_id=ORG,
            slack_user_id=USER,
            principal_id="gone-member",
            added_by="local-developer",
            created_at=now,
            updated_at=now,
        )
    )
    pworld.set_slack_settings(
        rules={
            "create_task": "user_only",
            "pr_review": "off",
            "run_workflow": "off",
            "ask_private": "off",
        }
    )
    pworld.queued_turn(f"{MARKER} task: ship it", origin={"action": "create_task"})

    _, event = _cancelled(pworld)

    assert event.metadata["cause"] == "member_inactive"


def test_the_guard_exposes_what_the_reply_step_needs(pworld: World) -> None:
    pworld.set_slack_settings(ask_enabled=False)
    pworld.queued_turn(f"{MARKER} anything")

    done = pworld.process()

    verdict = done.origin.verdict  # type: ignore[union-attr]
    assert (done.origin.channel_id, done.origin.thread_ts) == (  # type: ignore[union-attr]
        CHANNEL,
        "1700000000.000100",
    )
    assert verdict.outcome == "blocked_rule" and verdict.reason == "ask_disabled"
    assert verdict.message == messages.ASK_DISABLED and verdict.answer_ephemeral
    assert MARKER not in done.model_dump_json()
    assert SLACK not in verdict.tools_needed


def test_a_normal_turn_is_untouched_by_the_slack_pipeline(pworld: World) -> None:
    _open_jira(pworld)
    task = pworld.runtime.task_service.create_task(
        agent_id=pworld.agent_id, title="Plain", initial_message="what is WEAVES-1?"
    )

    done = pworld.process()

    assert done.task_id == task.task_id and done.origin is None
    assert done.status is TaskTurnStatus.SUCCEEDED
    assert pworld.models.planner_calls == 0 and not _decisions(pworld)
    # Every allowed capability ran, exactly as before this slice.
    assert pworld.connectors.count("jira", "data") == 1
    assert pworld.connectors.count("slack", "data") == 1


def test_a_slack_turn_without_a_guard_runs_no_connector(pworld: World) -> None:
    _open_jira(pworld)
    pworld.runtime.task_worker_service.turn_guard = None
    pworld.queued_turn(f"{MARKER} what is WEAVES-1?")

    done = pworld.process()

    assert done.status is TaskTurnStatus.SUCCEEDED
    assert done.origin is not None and done.origin.capability_ids is None
    assert pworld.connectors.total() == 0  # fail closed: no approved list, no tools


def test_the_heartbeat_does_not_overwrite_the_verdict(
    pworld: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    _open_jira(pworld)
    _plan(pworld, JIRA)
    pworld.runtime.task_worker_service.heartbeat_interval_seconds = 0.005
    original = pworld.runtime.agent_execution_service.run_agent

    def slow(*args, **kwargs):
        time.sleep(0.06)
        return original(*args, **kwargs)

    monkeypatch.setattr(pworld.runtime.agent_execution_service, "run_agent", slow)
    pworld.queued_turn(f"{MARKER} what is WEAVES-1?")

    done = pworld.process()

    stored = pworld.runtime.task_turns.get(done.turn_id)
    assert stored.origin is not None and stored.origin.capability_ids == (JIRA,)
    assert stored.origin.verdict is not None
