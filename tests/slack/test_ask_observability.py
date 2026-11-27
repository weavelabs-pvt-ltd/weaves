"""Request ids run end to end; no message text reaches a log line or a metric."""

from __future__ import annotations

import json
import logging

import pytest

from shared.metrics import REGISTRY
from tests.support.slack_ask_world import BOT_TOKEN, AskWorld
from tests.support.slack_pipeline_world import ORG

MARKER = "ZEBRA-QUESTION-TEXT-7731"


def test_the_request_id_follows_the_mention_everywhere(aworld: AskWorld) -> None:
    event = aworld.mention(f"{MARKER} what does idempotent mean?")
    aworld.dispatch()
    turn = aworld.work()
    aworld.reply()

    expected = f"slack-{event.event_id}"
    row = aworld.latest()
    assert row.request_id == expected and turn.request_id == expected
    decisions = [
        e
        for e in aworld.runtime.audit_events.list_scoped(ORG)
        if e.action == "slack.decision"
    ]
    assert [d.request_id for d in decisions] == [expected]


def test_no_message_text_or_token_in_logs_metrics_or_audit(
    aworld: AskWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    aworld.open_audience()
    aworld.mention(f"{MARKER} what does idempotent mean?")
    aworld.mention("help")
    aworld.mention(f"task: {MARKER}")
    aworld.dispatch()
    aworld.work()
    aworld.reply()
    aworld.slack.faults["chat.postMessage"] = ["500"]
    aworld.mention(f"{MARKER} again", thread_ts="1699999001.000100")

    logs = "\n".join(
        record.getMessage() + json.dumps(getattr(record, "__dict__", {}), default=str)
        for record in caplog.records
    )
    metrics = REGISTRY.render()
    audit = json.dumps(
        [
            e.model_dump(mode="json")
            for e in aworld.runtime.audit_events.list_scoped(ORG)
        ]
    )
    rows = json.dumps(
        [r.model_dump(mode="json") for r in aworld.runtime.slack_threads.list()]
    )

    for haystack in (logs, metrics, audit, rows):
        assert MARKER not in haystack
        assert BOT_TOKEN not in haystack
    assert "slack_dispatch_total" in metrics and "slack_reply_total" in metrics


def test_the_new_metrics_carry_no_organization_label(aworld: AskWorld) -> None:
    aworld.ask("what does idempotent mean?")

    series = [
        line
        for line in REGISTRY.render().splitlines()
        if line.startswith(
            ("slack_dispatch_total", "slack_reply_total", "slack_posts_total")
        )
    ]
    assert series
    assert all("org" not in line.split("{", 1)[-1] for line in series)


def test_the_bot_token_is_only_ever_in_the_authorization_header(
    aworld: AskWorld,
) -> None:
    aworld.ask("what does idempotent mean?")

    assert BOT_TOKEN not in json.dumps(aworld.slack.calls)
