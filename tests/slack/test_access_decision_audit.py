"""The slack.decision record shape and the audit-derived Ask usage."""

from __future__ import annotations

import json
from datetime import timedelta

from infrastructure.persistence.correlated_audit import CorrelatedInMemoryAudit
from modules.slack.access.decision_audit import (
    ASK_COUNTED_TARGET,
    DECISION_ACTION,
    EXECUTE_ALLOWED_ACTION,
    EXECUTE_BLOCKED_ACTION,
    DecisionContext,
    DecisionRecorder,
    PlannerRecord,
    build_decision_event,
)
from modules.slack.access.engine import evaluate
from modules.slack.access.models import Action, ToolHealth
from modules.slack.access.ratelimit import AuditAskUsageReader, check_ask_limits
from tests.support.slack_access_cases import NOW, request, settings, tool

ORG = "org-1"


def context(user: str = "U0ASKER01", channel: str = "C0CHAN001", n: int = 1):
    return DecisionContext(
        org_id=ORG,
        event_id=f"Ev{n:04d}",
        request_id=f"slack-Ev{n:04d}",
        slack_user_id=user,
        channel_id=channel,
        principal_id=None,
    )


def new_audit() -> CorrelatedInMemoryAudit:
    return CorrelatedInMemoryAudit("audit_event_id")


def test_decision_event_carries_ids_and_codes_but_no_text_or_data():
    decision = evaluate(
        request(
            Action.ASK,
            settings=settings(audience={"inst-jira": "everyone"}),
            tools=(tool(health=ToolHealth.ERROR),),
        )
    )
    event = build_decision_event(
        decision,
        context(),
        NOW,
        planner=PlannerRecord(True, "valid", "profile-1", 120),
        considered=("jira.search", "notion.search"),
    )
    meta = json.loads(event.model_dump_json())["metadata"]
    assert event.action == DECISION_ACTION
    assert meta["tier"] == "ask" and meta["outcome"] == "refused_unavailable"
    assert meta["reason"] == "tool_unavailable" and meta["mode"] == "strict"
    assert meta["settings_version"] == 7 and meta["event_id"] == "Ev0001"
    assert meta["tools"] == [
        {
            "tool_id": "jira.search",
            "installation_id": "inst-jira",
            "green": False,
            "reason": "tool_unavailable",
        }
    ]
    assert meta["planner"] == {
        "used": True,
        "status": "valid",
        "profile_id": "profile-1",
        "usage_units": 120,
    }
    assert meta["tools_considered"] == ["jira.search", "notion.search"]
    blob = event.model_dump_json().lower()
    for forbidden in ("question", "message_text", "text", 'token":', "secret"):
        assert forbidden not in blob
    assert event.request_id == "slack-Ev0001"
    assert event.actor_principal_id == "slack:U0ASKER01"


def test_allowed_ask_is_the_counted_kind_and_denials_are_not():
    allowed = build_decision_event(evaluate(request()), context(), NOW)
    blocked = build_decision_event(
        evaluate(request(settings=settings(ask_enabled=False))), context(), NOW
    )
    assert allowed.target_type == ASK_COUNTED_TARGET
    assert blocked.target_type != ASK_COUNTED_TARGET


def test_blocked_repeats_merge_into_one_event_with_a_counter():
    audit = new_audit()
    recorder = DecisionRecorder(audit)
    denied = evaluate(request(Action.CREATE_TASK))
    for i in range(4):
        recorder.record(denied, context(n=i), NOW + timedelta(seconds=i))
    rows = audit.list_scoped(ORG)
    decisions = [r for r in rows if r.action == DECISION_ACTION]
    attempts = [r for r in rows if r.action == EXECUTE_BLOCKED_ACTION]
    assert len(decisions) == len(attempts) == 1
    assert decisions[0].metadata["repeat_count"] == 4
    assert attempts[0].metadata["repeat_count"] == 4
    # A different person or a later bucket is a separate event.
    recorder.record(denied, context(user="U0OTHER02", n=9), NOW)
    recorder.record(denied, context(n=10), NOW + timedelta(minutes=11))
    assert len([r for r in audit.list_scoped(ORG) if r.action == DECISION_ACTION]) == 3


def test_every_allowed_request_gets_its_own_event():
    audit = new_audit()
    recorder = DecisionRecorder(audit)
    allowed = evaluate(request())
    for i in range(3):
        recorder.record(allowed, context(n=i), NOW)
    assert len(audit.list_scoped(ORG)) == 3
    executed = evaluate(
        request(
            Action.CREATE_TASK, settings=settings(rule="user_only"), user_listed=True
        )
    )
    recorder.record(executed, context(n=20), NOW)
    assert [r.action for r in audit.list_scoped(ORG)].count(EXECUTE_ALLOWED_ACTION) == 1


def test_ask_usage_reader_counts_only_allowed_asks_in_window():
    audit = new_audit()
    recorder = DecisionRecorder(audit)
    allowed = evaluate(request())
    recorder.record(allowed, context(n=1), NOW - timedelta(minutes=5))
    recorder.record(allowed, context(user="U0OTHER02", n=2), NOW - timedelta(minutes=4))
    recorder.record(
        allowed, context(channel="C0ELSE001", n=3), NOW - timedelta(minutes=3)
    )
    recorder.record(allowed, context(n=4), NOW - timedelta(minutes=30))
    recorder.record(allowed, context(n=5), NOW - timedelta(days=2))
    recorder.record(evaluate(request(Action.CREATE_TASK)), context(n=6), NOW)
    usage = AuditAskUsageReader(audit).load(ORG, "U0ASKER01", "C0CHAN001", NOW)
    assert len(usage.user) == 2  # n=1 and the other-channel n=3
    assert len(usage.channel) == 2  # n=1 and the other user's n=2
    assert len(usage.org) == 4  # all allowed asks inside the day
    assert (
        AuditAskUsageReader(audit).load("org-2", "U0ASKER01", "C0CHAN001", NOW).org
        == ()
    )


def test_reader_feeds_the_engine_end_to_end():
    audit = new_audit()
    recorder = DecisionRecorder(audit)
    allowed = evaluate(request())
    for i in range(5):
        recorder.record(allowed, context(n=i), NOW - timedelta(minutes=9 - i))
    reader = AuditAskUsageReader(audit)
    decision = evaluate(
        request(ask_usage=lambda: reader.load(ORG, "U0ASKER01", "C0CHAN001", NOW))
    )
    assert decision.reason is not None and decision.reason.value == "rate_limit_user"
    assert decision.retry_after_seconds == 60
    verdict = check_ask_limits(
        settings().ask_limits, reader.load(ORG, "U0X", "C0X", NOW), NOW
    )
    assert not verdict.limited
