"""Try again: the gates, the limits, the expiry and the text fallback."""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.v1 import TaskTurnStatus
from modules.slack import copy
from modules.slack.access import messages
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_pipeline_world import ORG, USER
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
    failed as _failed,
)
from tests.support.slack_press_helpers import (
    public as _public,
)
from tests.support.slack_press_helpers import (
    whispers as _whispers,
)


def _answering(w: AskWorld, monkeypatch: pytest.MonkeyPatch, original: object) -> None:
    monkeypatch.setattr(w.runtime.agent_execution_service, "run_agent", original)


def test_try_again_runs_the_stored_request_as_a_new_turn_through_the_gates(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = aworld.runtime.agent_execution_service.run_agent
    _failed(aworld, monkeypatch)
    row = aworld.latest()
    counted = len([e for e in aworld.runtime.audit_events.list_scoped(ORG)])
    _answering(aworld, monkeypatch, original)

    aworld.press("retry")
    aworld.interact()

    retried = aworld.latest()
    assert retried.retries == 1 and retried.turn_id != row.turn_id
    assert retried.run_ref != row.run_ref and retried.run_state == "queued"
    assert retried.reply_state == "pending" and retried.answer_ts is None
    new = aworld.runtime.task_turns.get(retried.turn_id)
    assert new.status is TaskTurnStatus.QUEUED
    assert new.idempotency_key == f"slack:T0FAKE001:retry:{row.run_ref}:1"
    assert new.origin is not None and new.origin.slack_user_id == USER
    # Same hidden conversation, the failed question again, no new task.
    assert retried.task_id == row.task_id and len(aworld.runtime.tasks.list()) == 1
    (message,) = _public(aworld)  # the failure message is the working message now
    assert message["text"] == copy.RETRYING
    assert aworld.buttons(message) == [("weaves.stop", retried.run_ref)]
    assert retried.status_ts == message["ts"] and retried.posts == 1

    aworld.work()
    aworld.reply()

    status, answer = _public(aworld)
    assert status["text"].startswith(":white_check_mark: Done")
    assert "From general knowledge" in answer["text"]
    assert aworld.latest().run_state == "done"
    decisions = [
        e
        for e in aworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.decision"
    ]
    assert len(decisions) == 2  # the retry counts against the Ask limits
    assert len(aworld.runtime.audit_events.list_scoped(ORG)) > counted


def test_try_again_is_limited_to_three_and_the_button_goes_away(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failed(aworld, monkeypatch)  # run_agent keeps failing
    for n in range(1, 4):
        aworld.press("retry")
        aworld.interact()
        aworld.work()
        aworld.reply()
        assert aworld.latest().retries == n

    (message,) = _public(aworld)
    assert message["text"].startswith(":warning: I couldn't finish this")
    assert aworld.buttons(message) == []  # three retries used: no fourth button

    aworld.mention("retry", thread_ts=aworld.latest().thread_ts)
    aworld.dispatch()
    assert _whispers(aworld)[-1] == copy.RETRY_LIMIT
    assert aworld.latest().retries == 3


def test_try_again_by_someone_else_is_refused(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _admin(aworld, monkeypatch)
    _failed(aworld, monkeypatch)

    aworld.press("retry", user=OTHER)
    aworld.interact()

    assert _whispers(aworld) == [copy.ONLY_REQUESTER_RETRY]
    assert aworld.latest().retries == 0
    assert _audit(aworld)[0].metadata["reason"] == "not_admin"

    aworld.press("retry", user=ADMIN)  # an admin may
    aworld.interact()
    assert aworld.latest().retries == 1 and _audit(aworld)[-1].metadata["reason"] == (
        "admin"
    )


def test_try_again_passes_every_gate_again(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failed(aworld, monkeypatch)
    aworld.runtime.slack_access_service.set_paused(True)

    aworld.press("retry")
    aworld.interact()

    assert _whispers(aworld) == [messages.UNAVAILABLE]  # the generic message
    assert aworld.latest().retries == 0 and len(aworld.runtime.task_turns.list()) == 1
    assert _audit(aworld)[0].metadata["outcome"] == "blocked"


def test_try_again_respects_the_ask_limit(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contracts.v1.slack_access import AskLimits

    _failed(aworld, monkeypatch)
    aworld.world.set_slack_settings(
        ask_limits=AskLimits(user_per_10_min=1).model_dump()
    )

    aworld.press("retry")
    aworld.interact()

    assert _whispers(aworld)[0].startswith(
        ":hourglass_flowing_sand: You're asking a lot; try again in"
    )
    assert aworld.latest().retries == 0


def test_try_again_is_refused_for_a_request_that_did_not_fail(
    aworld: AskWorld,
) -> None:
    aworld.ask("what does idempotent mean?")
    row = aworld.latest()

    aworld.press("retry", ref=row.run_ref, message_ts=row.status_ts or row.answer_ts)
    aworld.interact()

    assert _whispers(aworld) == [copy.NO_LONGER_AVAILABLE]
    assert aworld.latest().retries == 0


def test_try_again_expires_after_24_hours(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failed(aworld, monkeypatch)
    later = aworld.latest().finished_at + timedelta(hours=24, minutes=1)
    aworld.runtime.slack_interactions._clock = lambda: later

    aworld.press("retry")
    aworld.interact()

    assert _whispers(aworld) == [copy.NO_LONGER_AVAILABLE]
    assert _audit(aworld)[0].metadata["reason"] == "expired"
    assert aworld.latest().retries == 0


def test_a_second_press_on_try_again_cannot_start_a_second_run(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failed(aworld, monkeypatch)
    row = aworld.latest()
    ref, ts = row.run_ref, row.answer_ts

    aworld.press("retry", ref=ref, message_ts=ts)
    aworld.press("retry", ref=ref, message_ts=ts)
    aworld.interact()

    assert aworld.latest().retries == 1
    assert len(aworld.runtime.task_turns.list()) == 2
    assert _whispers(aworld) == [copy.NO_LONGER_AVAILABLE]  # the old reference is dead


def test_try_again_after_a_timeout_cancels_the_run_that_is_still_going(
    aworld: AskWorld,
) -> None:
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    row = aworld.latest()
    aworld.at(row.requested_at + timedelta(seconds=12))
    aworld.reply()
    aworld.at(row.requested_at + timedelta(minutes=16))
    aworld.reply()  # the deadline: the working message turns into the failure
    assert aworld.latest().run_state == "failed"
    assert aworld.runtime.task_turns.get(row.turn_id).status is TaskTurnStatus.QUEUED

    aworld.press("retry")
    aworld.interact()

    assert aworld.runtime.task_turns.get(row.turn_id).status is TaskTurnStatus.CANCELLED
    assert aworld.latest().retries == 1 and aworld.latest().run_state == "queued"


def test_text_retry_runs_the_failed_request_again(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = aworld.runtime.agent_execution_service.run_agent
    _failed(aworld, monkeypatch)
    _answering(aworld, monkeypatch, original)

    aworld.mention("try again", thread_ts=aworld.latest().thread_ts)
    aworld.dispatch()

    assert aworld.latest().retries == 1
    assert aworld.runtime.task_turns.get(aworld.latest().turn_id).status is (
        TaskTurnStatus.QUEUED
    )
    assert _audit(aworld)[0].metadata["source"] == "text"
