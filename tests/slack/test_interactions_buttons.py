"""The Stop and Try again buttons: their Block Kit shape and the audit record."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from modules.slack import copy
from modules.slack.reply import compose
from tests.support.slack_ask_world import AskWorld
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
    running as _running,
)


def test_the_working_message_carries_a_stop_button_with_an_opaque_reference(
    aworld: AskWorld,
) -> None:
    _running(aworld)

    (message,) = _public(aworld)
    row = aworld.latest()
    blocks = json.loads(message["blocks"])
    assert [b["type"] for b in blocks] == ["section", "actions"]
    assert blocks[0]["text"]["text"] == copy.CHECKING == message["text"]
    (button,) = blocks[1]["elements"]
    assert button["action_id"] == "weaves.stop"
    assert button["text"] == {"type": "plain_text", "text": "Stop"}
    assert button["value"] == row.run_ref and len(row.run_ref) >= 20
    # Never one of our ids: nothing in the value names the organization or a row.
    for ours in (row.id, row.org_id, row.task_id, row.turn_id, row.team_id):
        assert ours not in button["value"]


def test_the_taking_longer_message_keeps_its_stop_button(aworld: AskWorld) -> None:
    _running(aworld)
    aworld.at(aworld.latest().requested_at + timedelta(minutes=5, seconds=5))
    aworld.reply()

    (message,) = _public(aworld)
    assert message["text"] == copy.TAKING_LONGER
    assert aworld.buttons(message) == [("weaves.stop", aworld.latest().run_ref)]


def test_a_failure_message_has_try_again_and_no_error_text(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failed(aworld, monkeypatch)

    (message,) = _public(aworld)
    row = aworld.latest()
    assert message["text"] == copy.failure(compose.short_ref(row.turn_id))
    assert aworld.buttons(message) == [("weaves.retry", row.run_ref)]
    assert "exploded" not in json.dumps(message)


def test_buttons_are_removed_when_the_message_becomes_the_receipt(
    aworld: AskWorld,
) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    start = aworld.latest().requested_at
    aworld.at(start + timedelta(seconds=12))
    aworld.reply()
    (status,) = _public(aworld)
    assert aworld.buttons(status) != []

    aworld.work()
    aworld.at(start + timedelta(seconds=41))
    aworld.reply()

    status, answer = _public(aworld)
    assert status["text"].startswith(":white_check_mark: Done in ")
    assert aworld.buttons(status) == [] and status["blocks"] == "[]"
    assert "blocks" not in answer  # the answer itself never has a button


def test_a_private_reply_edit_leaves_no_button_behind(aworld: AskWorld) -> None:
    assert aworld.world.jira is not None
    aworld.world.snapshot(aworld.world.jira, "healthy")
    from tests.support.slack_pipeline_world import JIRA

    aworld.plan(JIRA)  # Jira stays closed: the answer is a private refusal
    aworld.mention("what is WEAVES-1?")
    aworld.dispatch()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=12))
    aworld.reply()
    aworld.work()
    aworld.reply()

    assert _public(aworld) == []


def test_button_text_goes_through_the_same_scrubber(aworld: AskWorld) -> None:
    _running(aworld)
    poster = aworld.runtime.slack_reply_tick._delivery._poster

    with pytest.raises(Exception, match="scrubber"):
        poster.edit(
            aworld.runtime.slack_store.find_by_team("T0FAKE001"),
            "C0CHAN001",
            "1.1",
            "<!channel> raw markup",
            button=None,
        )


def test_the_audit_record_has_no_message_text_and_no_reference(
    aworld: AskWorld,
) -> None:
    aworld.mention("tell me about ZEBRA-QUESTION-TEXT-7731")
    aworld.dispatch()
    aworld.at(aworld.latest().requested_at + timedelta(seconds=12))
    aworld.reply()
    ref = aworld.latest().run_ref
    aworld.press("stop")
    aworld.interact()

    (event,) = _audit(aworld)
    dumped = event.model_dump_json()
    assert "ZEBRA" not in dumped and ref not in dumped
    assert set(event.metadata) == {
        "action",
        "source",
        "outcome",
        "reason",
        "slack_user_id",
        "principal_id",
        "channel_id",
        "run",
    }
    assert len(event.metadata["run"]) == 12
    assert [r.text for r in aworld.inbox()] == [None] * len(aworld.inbox())
