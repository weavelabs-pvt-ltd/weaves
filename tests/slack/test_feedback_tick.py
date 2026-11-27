"""Reaction feedback: which reactions count, latest wins, states, idempotency."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1 import OrganizationLifecycleState
from modules.slack.feedback.reaction import feedback_value
from shared.metrics import REGISTRY
from tests.support.slack_ask_world import AskWorld
from tests.support.slack_feedback_world import (
    BOT_USER,
    CHANNEL,
    USER,
    answered,
    feedback,
    inbox_state,
    map_member,
    react,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def tick(aworld: AskWorld) -> int:
    return int(aworld.runtime.slack_feedback_tick.run())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("+1", 1),
        ("thumbsup", 1),
        ("+1::skin-tone-2", 1),
        ("thumbsup::skin-tone-6", 1),
        ("-1", -1),
        ("thumbsdown", -1),
        ("-1::skin-tone-4", -1),
        ("+1::skin-tone-1", None),
        ("+1::skin-tone-7", None),
        ("+1::other", None),
        ("tada", None),
        ("eyes", None),
        ("+1 ", None),
        ("", None),
        (None, None),
    ],
)
def test_only_thumbs_with_a_slack_skin_tone_suffix_are_feedback(
    name: str | None, value: int | None
) -> None:
    assert feedback_value(name) == value


def test_the_askers_thumbs_up_on_our_answer_is_stored_without_text_or_slack_id(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    event = react(aworld, thread.answer_ts)

    assert tick(aworld) == 1

    (row,) = feedback(aworld)
    assert (row.value, row.source, row.turn_id) == (1, "slack", thread.turn_id)
    assert row.task_id == thread.task_id and row.request_id == thread.request_id
    assert row.answer_ref == f"{CHANNEL}:{thread.answer_ts}"
    assert row.asker_kind == "hashed" and row.asker_ref.startswith("hmac:")
    assert row.at == event.received_at
    assert row.agent_run_id is not None and row.agent_id and row.model_id
    stored = row.model_dump_json()
    assert USER not in stored and "idempotency" not in stored
    assert inbox_state(aworld, event) == ("done", None)


def test_the_latest_reaction_wins_and_an_older_delivery_never_overwrites(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    react(aworld, thread.answer_ts, reaction="+1", received_at=NOW)
    react(
        aworld, thread.answer_ts, reaction="-1", received_at=NOW + timedelta(minutes=1)
    )
    tick(aworld)
    (row,) = feedback(aworld)
    assert row.value == -1 and row.at == NOW + timedelta(minutes=1)

    older = react(
        aworld, thread.answer_ts, reaction="+1", received_at=NOW - timedelta(hours=1)
    )
    tick(aworld)
    (row,) = feedback(aworld)
    assert row.value == -1  # the late arrival of an older reaction changed nothing
    assert inbox_state(aworld, older) == ("done", None)

    react(
        aworld,
        thread.answer_ts,
        reaction="+1::skin-tone-3",
        received_at=NOW + timedelta(days=1),
    )
    tick(aworld)
    (row,) = feedback(aworld)
    assert row.value == 1


def test_one_row_per_turn_and_asker_whatever_the_number_of_reactions(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    for n in range(5):
        react(
            aworld,
            thread.answer_ts,
            reaction="+1" if n % 2 else "-1",
            received_at=NOW + timedelta(seconds=n),
        )
    assert tick(aworld) == 5
    assert len(feedback(aworld)) == 1


def test_a_redelivered_event_is_stored_once_and_replaying_it_changes_nothing(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    event = react(aworld, thread.answer_ts, event_id="EvSAME")
    assert aworld.runtime.slack_store.record_event(event) is False  # Slack retry
    tick(aworld)
    before = feedback(aworld)
    assert tick(aworld) == 0
    # a crash after the write but before the settle: the replay is a no-op
    tick_object = aworld.runtime.slack_feedback_tick
    assert (
        tick_object._record(thread, event, 1, tick_object._refs.resolve(USER))
        == "repeated"
    )
    assert feedback(aworld) == before


@pytest.mark.parametrize(
    ("label", "reason"),
    [
        ("other_user", "not_the_asker"),
        ("unknown_message", "not_our_answer"),
        ("working_message", "not_our_answer"),
        ("other_channel", "not_our_answer"),
        ("bots_own", "own_reaction"),
        ("other_emoji", "not_feedback"),
    ],
)
def test_everything_else_is_dropped_with_a_reason_and_stores_nothing(
    aworld: AskWorld, label: str, reason: str
) -> None:
    thread = answered(aworld)
    ts = {
        "unknown_message": "1700000999.000100",
        "working_message": thread.status_ts,
    }.get(label, thread.answer_ts)
    kwargs = {
        "other_user": {"user": "U0SOMEONE"},
        "other_channel": {"channel": "C0OTHER01"},
        "bots_own": {"user": BOT_USER},
        "other_emoji": {"reaction": "tada"},
    }.get(label, {})
    event = react(aworld, ts, **kwargs)

    assert tick(aworld) == 1

    assert feedback(aworld) == []
    assert inbox_state(aworld, event) == ("dropped", reason)


def test_a_reaction_on_a_private_answer_or_no_answer_yet_is_not_ours(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    aworld.runtime.slack_threads.put(thread.model_copy(update={"answer_ts": "private"}))
    event = react(aworld, "private")
    tick(aworld)
    assert feedback(aworld) == [] and inbox_state(aworld, event)[0] == "dropped"
    event = react(aworld, None)
    tick(aworld)
    assert inbox_state(aworld, event) == ("dropped", "not_our_answer")


def test_a_follow_up_answer_replaces_the_target_and_the_old_answer_is_not_rated(
    aworld: AskWorld,
) -> None:
    first = answered(aworld)
    aworld.mention("and again?", thread_ts=first.thread_ts)
    aworld.dispatch()
    aworld.work()
    aworld.reply()
    second = aworld.latest()
    assert second.answer_ts != first.answer_ts

    old = react(aworld, first.answer_ts)
    react(aworld, second.answer_ts, reaction="-1")
    tick(aworld)

    assert inbox_state(aworld, old) == ("dropped", "not_our_answer")
    (row,) = feedback(aworld)
    assert row.turn_id == second.turn_id and row.value == -1


def test_a_mapped_member_is_stored_by_principal_and_a_late_mapping_keeps_one_row(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    react(aworld, thread.answer_ts, reaction="+1", received_at=NOW)
    tick(aworld)
    (before,) = feedback(aworld)
    assert before.asker_kind == "hashed"

    map_member(aworld, USER, "local-developer")
    react(aworld, thread.answer_ts, reaction="-1", received_at=NOW + timedelta(hours=1))
    tick(aworld)

    (after,) = feedback(aworld)
    assert (after.asker_kind, after.asker_ref, after.value) == (
        "member",
        "local-developer",
        -1,
    )
    assert after.id != before.id and after.created_at <= after.updated_at
    assert USER not in after.model_dump_json()


def _inbox_after(aworld: AskWorld, ts: str, **kwargs: str) -> tuple[str, str | None]:
    event = react(aworld, ts, **kwargs)
    tick(aworld)
    return inbox_state(aworld, event)


def test_a_paused_bot_drops_the_reaction(aworld: AskWorld) -> None:
    thread = answered(aworld)
    aworld.runtime.slack_access_service.set_paused(True)
    assert _inbox_after(aworld, thread.answer_ts) == ("dropped", "bot_paused")
    assert feedback(aworld) == []


def test_a_quarantined_or_suspended_organization_drops_the_reaction(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = answered(aworld)
    monkeypatch.setattr(
        aworld.runtime.organization_lifecycle_service,
        "state_of",
        lambda org_id: OrganizationLifecycleState.SUSPENDED,
    )
    assert _inbox_after(aworld, thread.answer_ts) == ("dropped", "org_unavailable")
    assert feedback(aworld) == []


def test_a_suspended_or_uninstalled_installation_drops_the_reaction(
    aworld: AskWorld,
) -> None:
    thread = answered(aworld)
    store = aworld.runtime.slack_store
    store.set_status("slack-inst-1", "suspended", datetime.now(UTC))
    assert _inbox_after(aworld, thread.answer_ts) == (
        "dropped",
        "installation_inactive",
    )
    store.set_status("slack-inst-1", "uninstalled", datetime.now(UTC))
    assert _inbox_after(aworld, thread.answer_ts) == (
        "dropped",
        "installation_inactive",
    )
    assert feedback(aworld) == []


def test_the_platform_pause_and_the_slack_kill_switch_drop_the_reaction(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = answered(aworld)
    controls = aworld.runtime.platform_controls
    original, snapshot = controls.snapshot, controls.snapshot()
    monkeypatch.setattr(
        controls,
        "snapshot",
        lambda: type(snapshot)(**{**vars(snapshot), "admissions_paused": True}),
    )
    assert _inbox_after(aworld, thread.answer_ts) == ("dropped", "platform_paused")
    monkeypatch.setattr(controls, "snapshot", original)
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "false")
    assert _inbox_after(aworld, thread.answer_ts) == ("dropped", "slack_disabled")
    assert feedback(aworld) == []


def test_a_read_only_platform_holds_the_reaction_until_it_can_write(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = answered(aworld)
    controls = aworld.runtime.platform_controls
    original, snapshot = controls.snapshot, controls.snapshot()
    monkeypatch.setattr(
        controls,
        "snapshot",
        lambda: type(snapshot)(**{**vars(snapshot), "read_only": True}),
    )
    event = react(aworld, thread.answer_ts)
    assert tick(aworld) == 0 and inbox_state(aworld, event)[0] == "pending"
    monkeypatch.setattr(controls, "snapshot", original)
    assert tick(aworld) == 1 and len(feedback(aworld)) == 1


def test_a_row_that_keeps_failing_is_dropped_and_the_next_one_is_served(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    thread = answered(aworld)
    bad = react(aworld, thread.answer_ts, event_id="EvBAD", received_at=NOW)
    good = react(
        aworld,
        thread.answer_ts,
        event_id="EvGOOD",
        received_at=NOW + timedelta(seconds=1),
    )
    tick_object = aworld.runtime.slack_feedback_tick
    original = tick_object._record

    def broken(thread_row, event, value, asker):
        if event.event_id == "EvBAD":
            raise RuntimeError(f"boom {USER}")
        return original(thread_row, event, value, asker)

    monkeypatch.setattr(tick_object, "_record", broken)
    with caplog.at_level(logging.INFO):
        for _ in range(6):
            tick(aworld)
    assert inbox_state(aworld, bad) == ("dropped", "feedback_error")
    assert inbox_state(aworld, good)[0] == "done"
    assert USER not in caplog.text


def test_metrics_and_logs_carry_no_user_org_or_text(
    aworld: AskWorld, caplog: pytest.LogCaptureFixture
) -> None:
    thread = answered(aworld)
    react(aworld, thread.answer_ts, reaction="-1")
    react(aworld, thread.answer_ts, user="U0SOMEONE")
    audits = len(aworld.runtime.audit_events.list())
    with caplog.at_level(logging.INFO):
        tick(aworld)
    text = REGISTRY.render()
    lines = [ln for ln in text.splitlines() if ln.startswith("slack_feedback_total")]
    assert any('value="down"' in ln and 'outcome="recorded"' in ln for ln in lines)
    assert any('outcome="dropped_not_the_asker"' in ln for ln in lines)
    ours = " ".join(
        r.getMessage() for r in caplog.records if "slack_feedback" in r.getMessage()
    )
    assert ours  # the tick logged
    for secret in (USER, "U0SOMEONE", "idempotency", "local-org", thread.turn_id):
        assert all(secret not in ln for ln in lines), secret
        assert secret not in ours, secret
    assert len(aworld.runtime.audit_events.list()) == audits  # feedback writes none
