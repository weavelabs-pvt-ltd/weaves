"""The three Slack worker ticks: registered, isolated from the loop, pausable."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from apps.worker import ticks
from apps.worker.ticks import build_tick_registry


def _services(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(
        dispatch_slack=None,
        reply_slack=None,
        purge_slack=None,
        worker_paused=None,
        **fields,
    )


def test_the_ticks_run_every_second_and_retention_every_fifteen_minutes() -> None:
    registry = build_tick_registry(schedule_interval=5, lifecycle_interval=30)
    by_name = {t.name: t for t in registry.ticks}
    assert by_name["slack_dispatch"].interval_seconds == 1
    assert by_name["slack_reply"].interval_seconds == 1
    assert by_name["slack_qa_retention"].interval_seconds == 900


def test_a_slack_failure_never_disturbs_the_worker_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> int:
        raise RuntimeError("slack is down")

    services = _services()
    services.dispatch_slack = boom
    services.reply_slack = boom
    services.purge_slack = boom

    with caplog.at_level(logging.ERROR):
        ticks._slack_dispatch(services)  # type: ignore[arg-type]
        ticks._slack_reply(services)  # type: ignore[arg-type]
        ticks._slack_retention(services)  # type: ignore[arg-type]

    assert len([r for r in caplog.records if "tick failed" in r.getMessage()]) == 3


def test_an_absent_component_is_a_no_op() -> None:
    services = _services()
    ticks._slack_dispatch(services)  # type: ignore[arg-type]
    ticks._slack_reply(services)  # type: ignore[arg-type]
    ticks._slack_retention(services)  # type: ignore[arg-type]


def test_a_paused_worker_holds_the_dispatcher_but_not_the_replies() -> None:
    calls: list[str] = []
    services = _services()
    services.dispatch_slack = lambda: calls.append("dispatch") or 0  # type: ignore[func-returns-value]
    services.reply_slack = lambda: calls.append("reply") or 0  # type: ignore[func-returns-value]
    services.worker_paused = lambda: True

    ticks._slack_dispatch(services)  # type: ignore[arg-type]
    ticks._slack_reply(services)  # type: ignore[arg-type]

    assert calls == ["reply"]
    services.worker_paused = lambda: False
    ticks._slack_dispatch(services)  # type: ignore[arg-type]
    assert calls == ["reply", "dispatch"]
