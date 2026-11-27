"""A row that keeps failing is given up on and never starves the rows behind it."""

from __future__ import annotations

import pytest

from modules.slack.dispatch.dispatcher import MAX_FAILURES
from modules.slack.reply.tick import MAX_ROW_FAILURES
from tests.support.slack_ask_world import AskWorld


def test_a_mention_that_keeps_failing_is_dropped_and_newer_ones_still_run(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = aworld.mention("this one cannot be queued")
    good = aworld.mention("hello there")
    ask = aworld.runtime.slack_dispatcher._ask
    real = ask.run

    def flaky(installation, event, question, now):  # type: ignore[no-untyped-def]
        if event.event_id == bad.event_id:
            raise PermissionError("the installer lost the admin role")
        return real(installation, event, question, now)

    monkeypatch.setattr(ask, "run", flaky)

    for _ in range(MAX_FAILURES):
        aworld.dispatch()

    states = {e.event_id: (e.state, e.drop_reason) for e in aworld.inbox()}
    assert states[bad.event_id] == ("dropped", "dispatch_error")
    assert states[good.event_id][0] == "done"  # the cheap exit was never starved
    assert aworld.runtime.tasks.list() == ()


def test_a_reply_row_that_keeps_failing_ends_undeliverable(
    aworld: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    aworld.mention("what does idempotent mean?")
    aworld.dispatch()
    aworld.work()
    tick = aworld.runtime.slack_reply_tick

    def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("the database is unhappy about this one row")

    monkeypatch.setattr(tick, "_step", broken)
    for _ in range(MAX_ROW_FAILURES):
        aworld.reply()

    row = aworld.latest()
    assert row.reply_state == "undeliverable" and row.reply_error == "internal_error"
    assert aworld.slack.count("chat.postMessage") == 0
