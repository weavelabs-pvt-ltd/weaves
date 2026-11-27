"""Stop: who may press, the state machine, paused and uninstalled workspaces."""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.v1 import TaskTurnStatus
from modules.slack import copy
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_press_helpers import (
    ADMIN,
    OTHER,
)
from tests.support.slack_press_helpers import (
    admin_standing as _admin,
)
from tests.support.slack_press_helpers import (
    audit as _audit,
)
from tests.support.slack_press_helpers import (
    public as _public,
)
from tests.support.slack_press_helpers import (
    running as _running,
)
from tests.support.slack_press_helpers import (
    whispers as _whispers,
)


def test_the_requester_stops_the_run_and_the_message_becomes_stopped(
    aworld: AskWorld,
) -> None:
    _running(aworld)
    row = aworld.latest()

    assert aworld.press("stop") == "accepted"
    assert aworld.interact() == 1

    turn = aworld.runtime.task_turns.get(row.turn_id)
    assert turn.status is TaskTurnStatus.CANCELLED
    stored = aworld.latest()
    assert stored.run_state == "stopped" and stored.reply_state == "posted"
    (message,) = _public(aworld)
    assert message["text"] == copy.STOPPED and aworld.buttons(message) == []
    assert message["ts"] == row.status_ts and stored.answer_ts == row.status_ts
    assert _whispers(aworld) == []
    (event,) = _audit(aworld)
    assert event.metadata["outcome"] == "allowed" and event.metadata["reason"] == (
        "requester"
    )
    assert event.metadata["action"] == "stop" and event.metadata["source"] == "button"


def test_a_stopped_turn_is_never_answered_and_no_model_runs(aworld: AskWorld) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=12))
    aworld.reply()  # the queued notice with its button
    aworld.press("stop")
    aworld.interact()

    assert aworld.runtime.task_worker_service.process_next_turn("w") is None
    aworld.reply()
    aworld.reply()

    assert aworld.world.models.answer_calls == 0
    assert [m["text"] for m in _public(aworld)] == [copy.STOPPED]
    assert aworld.slack.count("chat.postMessage") == 1  # the queued notice, edited


def test_an_answer_that_was_ready_when_stop_landed_is_discarded(
    aworld: AskWorld,
) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()  # the turn succeeded, nothing was posted yet
    row = aworld.latest()
    assert row.answer_ts is None and row.status_ts is None

    aworld.mention("stop", thread_ts=row.thread_ts)  # no message exists: text only
    aworld.dispatch()
    aworld.reply()
    aworld.reply()

    assert [m["text"] for m in _public(aworld)] == [copy.STOPPED]
    stored = aworld.latest()
    assert stored.run_state == "stopped" and stored.reply_state == "posted"
    assert stored.posts == 1


def test_an_admin_may_stop_someone_elses_request(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(aworld, monkeypatch)
    _running(aworld)

    aworld.press("stop", user=ADMIN)
    aworld.interact()

    assert aworld.latest().run_state == "stopped"
    (event,) = _audit(aworld)
    assert event.metadata["reason"] == "admin"
    assert event.metadata["principal_id"] == "p-admin"
    assert event.actor_principal_id == "p-admin"


@pytest.mark.parametrize(
    ("user", "reason"), [(OTHER, "not_admin"), ("U0STRANGER", "not_mapped")]
)
def test_anyone_else_is_told_privately_and_nothing_changes(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch, user: str, reason: str
) -> None:
    _admin(aworld, monkeypatch)
    _running(aworld)
    row = aworld.latest()

    aworld.press("stop", user=user)
    aworld.interact()

    assert _whispers(aworld) == [copy.ONLY_REQUESTER_STOP]
    assert aworld.slack.ephemerals[0]["user"] == user
    assert aworld.runtime.task_turns.get(row.turn_id).status is TaskTurnStatus.RUNNING
    assert aworld.latest().run_state == "running"
    (event,) = _audit(aworld)
    assert event.metadata["outcome"] == "blocked"
    assert event.metadata["reason"] == reason


def test_a_double_press_says_already_stopped_and_audits_both(
    aworld: AskWorld,
) -> None:
    _running(aworld)

    aworld.press("stop")
    aworld.press("stop")  # a second click: its own action_ts
    assert aworld.interact() == 2

    assert _whispers(aworld) == [copy.ALREADY_STOPPED]
    assert [m["text"] for m in _public(aworld)] == [copy.STOPPED]
    reasons = sorted(e.metadata["reason"] for e in _audit(aworld))
    assert reasons == ["already_stopped", "requester"]
    assert aworld.slack.count("chat.update") == 1  # the one edit to "Stopped."


def test_a_replayed_delivery_is_one_press(aworld: AskWorld) -> None:
    _running(aworld)

    assert aworld.press("stop", action_ts="1700100001.000100") == "accepted"
    assert aworld.press("stop", action_ts="1700100001.000100") == "duplicate"
    aworld.interact()

    assert len(_audit(aworld)) == 1 and _whispers(aworld) == []


@pytest.mark.parametrize("forgery", ["ref", "message", "thread", "other_team"])
def test_a_forged_or_misplaced_button_finds_nothing(
    aworld: AskWorld, forgery: str
) -> None:
    _running(aworld)
    row = aworld.latest()
    args: dict = {}
    if forgery == "ref":
        args["ref"] = "A" * 22
    elif forgery == "message":
        args["message_ts"] = "1700999999.000100"  # not one of this run's messages
    elif forgery == "thread":
        args["thread_ts"] = "1700999999.000200"  # another thread
    else:
        args["ref"] = row.id.replace(":", "_")  # an id of ours as the value

    aworld.press("stop", **args)
    aworld.interact()

    assert _whispers(aworld) == [copy.NO_LONGER_AVAILABLE]
    assert aworld.runtime.task_turns.get(row.turn_id).status is TaskTurnStatus.RUNNING
    (event,) = _audit(aworld)
    assert event.metadata["outcome"] == "blocked" and event.metadata["reason"] == (
        "no_run"
    )


def test_a_button_of_a_finished_run_is_no_longer_available(aworld: AskWorld) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=12))
    aworld.reply()
    status_ts = aworld.latest().status_ts
    aworld.work()
    aworld.reply()  # answered: the working message is the receipt
    ref = aworld.latest().run_ref

    aworld.press("stop", ref=ref, message_ts=status_ts)
    aworld.interact()

    assert _whispers(aworld) == [copy.NO_LONGER_AVAILABLE]
    assert aworld.latest().run_state == "done"
    assert _audit(aworld)[0].metadata["reason"] == "not_running"


def test_a_press_after_a_follow_up_cannot_act_on_the_new_request(
    aworld: AskWorld,
) -> None:
    aworld.ask("what does idempotent mean?")
    old_ref = aworld.latest().run_ref
    thread = aworld.latest().thread_ts
    aworld.mention("and more?", thread_ts=thread)
    aworld.dispatch()
    assert aworld.latest().run_ref != old_ref  # a new request, new reference

    aworld.press("stop", ref=old_ref)
    aworld.interact()

    assert _whispers(aworld) == [copy.NO_LONGER_AVAILABLE]
    assert aworld.latest().run_state == "queued"


# -- Stop while paused, quarantined, uninstalled


def test_stop_still_works_while_the_bot_is_paused(aworld: AskWorld) -> None:
    _running(aworld)
    aworld.runtime.slack_access_service.set_paused(True)

    aworld.press("stop")
    aworld.interact()

    assert aworld.latest().run_state == "stopped"
    assert [m["text"] for m in _public(aworld)] == [copy.STOPPED]


def test_stop_still_works_for_a_quarantined_organization(aworld: AskWorld) -> None:
    _running(aworld)
    held = aworld.runtime.slack_store.find_by_team("T0FAKE001")
    assert held is not None
    from shared.clock import utc_now

    aworld.runtime.slack_store.set_status(held.installation_id, "suspended", utc_now())

    assert aworld.press("stop") == "accepted"
    aworld.interact()

    assert aworld.latest().run_state == "stopped"


def test_after_uninstall_a_press_is_acked_and_dropped(aworld: AskWorld) -> None:
    _running(aworld)
    held = aworld.runtime.slack_store.find_by_team("T0FAKE001")
    assert held is not None
    from shared.clock import utc_now

    aworld.press("stop")  # stored just before the app was removed
    aworld.runtime.slack_store.set_status(
        held.installation_id, "uninstalled", utc_now()
    )
    assert aworld.press("stop") == "unknown_team"  # acked, nothing stored
    aworld.interact()

    assert aworld.latest().run_state == "running"
    presses = [r for r in aworld.inbox() if r.event_type == "interaction"]
    assert [r.state for r in presses] == ["dropped"]
    assert presses[0].drop_reason == "installation_inactive" and presses[0].text is None
    assert _audit(aworld) == [] and _whispers(aworld) == []


def test_a_disabled_deployment_acks_and_drops(aworld: AskWorld) -> None:
    _running(aworld)

    assert aworld.press("stop", enabled=False) == "disabled"
    assert [r for r in aworld.inbox() if r.event_type == "interaction"] == []


def test_text_stop_has_the_same_authorization_as_the_button(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(aworld, monkeypatch)
    _running(aworld)
    thread = aworld.latest().thread_ts

    aworld.mention("stop", thread_ts=thread, user=OTHER)
    aworld.dispatch()
    assert _whispers(aworld) == [copy.ONLY_REQUESTER_STOP]
    assert aworld.latest().run_state == "running"

    aworld.mention("stop", thread_ts=thread)
    aworld.dispatch()
    assert aworld.latest().run_state == "stopped"
    assert [m["text"] for m in _public(aworld)] == [copy.STOPPED]
    assert [e.metadata["source"] for e in _audit(aworld)] == ["text", "text"]
    assert [e.metadata["outcome"] for e in _audit(aworld)] == ["blocked", "allowed"]


def test_text_stop_with_nothing_running_says_so_privately(aworld: AskWorld) -> None:
    aworld.mention("stop")
    aworld.dispatch()

    assert _whispers(aworld) == [copy.NO_ACTIVITY]
    assert aworld.threads() == []


def test_text_stop_works_while_the_bot_is_paused(aworld: AskWorld) -> None:
    _running(aworld)
    aworld.runtime.slack_access_service.set_paused(True)

    aworld.mention("stop", thread_ts=aworld.latest().thread_ts)
    aworld.dispatch()

    assert aworld.latest().run_state == "stopped"
