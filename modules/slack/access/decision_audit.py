"""The ``slack.decision`` audit record: one per request, never any message text.

Every decision the engine makes is recorded the same way by the later slices
(dispatcher at claim time, pre-run guard at pickup). The record carries ids,
reason codes and counts only; it has no question text, no connector data and no
token. Execute requests additionally write ``slack.execute.allowed`` or
``slack.execute.blocked`` (the "recent attempts" list reads those). Blocked
repeats from the same person for the same reason merge into one event with a
counter per ten-minute bucket, so a stuck user cannot flood the trail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import AuditEvent, AuditTargetScope
from modules.slack.access.models import Decision, Outcome
from shared.clock import new_id

DECISION_ACTION = "slack.decision"
EXECUTE_ALLOWED_ACTION = "slack.execute.allowed"
EXECUTE_BLOCKED_ACTION = "slack.execute.blocked"
ASK_COUNTED_TARGET = "slack_ask_allowed"  # an allowed Ask: rate limits count these
DECISION_TARGET = "slack_decision"
EXECUTE_TARGET = "slack_execute"
MERGE_BUCKET_SECONDS = 600
_MAX_TOOLS = 40


@dataclass(frozen=True)
class DecisionContext:
    """Who asked and which Slack event it was (ids only)."""

    org_id: str
    event_id: str
    request_id: str
    slack_user_id: str
    channel_id: str
    principal_id: str | None = None


@dataclass(frozen=True)
class PlannerRecord:
    """What the Ask planner did (filled in by the pipeline slice)."""

    used: bool = False
    status: str = "skipped"  # valid | invalid | skipped | timeout
    profile_id: str | None = None
    usage_units: int | None = None


def _tool_rows(decision: Decision) -> list[dict[str, Any]]:
    return [
        {
            "tool_id": r.tool_id,
            "installation_id": r.installation_id,
            "green": r.green,
            "reason": r.reason.value if r.reason else None,
        }
        for r in decision.tool_results[:_MAX_TOOLS]
    ]


def decision_metadata(
    decision: Decision,
    context: DecisionContext,
    *,
    planner: PlannerRecord | None = None,
    considered: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The metadata of one decision record (JSON-safe, no text, no data)."""
    planner = planner or PlannerRecord()
    return {
        "tier": "execute" if decision.action.is_execute else "ask",
        "action": decision.action.value,
        "outcome": decision.outcome.value,
        "allowed": decision.allowed,
        "reason": decision.reason.value if decision.reason else None,
        "mode": decision.mode.value,
        "checks": list(decision.checks),
        "settings_version": decision.settings_version,
        "slack_user_id": context.slack_user_id,
        "channel_id": context.channel_id,
        "event_id": context.event_id,
        "tools_considered": list(considered[:_MAX_TOOLS]),
        "tools_needed": [r.tool_id for r in decision.tool_results[:_MAX_TOOLS]],
        "tools": _tool_rows(decision),
        "planner": {
            "used": planner.used,
            "status": planner.status,
            "profile_id": planner.profile_id,
            "usage_units": planner.usage_units,
        },
        "answer_ephemeral": decision.answer_ephemeral,
        "retry_after_seconds": decision.retry_after_seconds,
        "repeat_count": 1,
    }


def _merge_id(
    action: str, decision: Decision, context: DecisionContext, now: datetime
) -> str:
    bucket = int(now.timestamp()) // MERGE_BUCKET_SECONDS
    reason = decision.reason.value if decision.reason else ""
    raw = "|".join(
        (
            action,
            context.org_id,
            context.slack_user_id,
            decision.action.value,
            decision.outcome.value,
            reason,
            str(bucket),
        )
    )
    return "slack-merge-" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def _event(
    *,
    event_id: str,
    action: str,
    target_type: str,
    decision: Decision,
    context: DecisionContext,
    now: datetime,
    metadata: dict[str, Any],
) -> AuditEvent:
    return AuditEvent(
        audit_event_id=event_id,
        org_id=context.org_id,
        target_scope=AuditTargetScope.ORGANIZATION,
        actor_principal_id=context.principal_id or f"slack:{context.slack_user_id}",
        action=action,
        target_type=target_type,
        target_id=context.request_id,
        summary=f"Slack {decision.action.value} {decision.outcome.value}",
        request_id=context.request_id,
        metadata=metadata,
        created_at=now,
    )


def build_decision_event(
    decision: Decision,
    context: DecisionContext,
    now: datetime,
    *,
    planner: PlannerRecord | None = None,
    considered: tuple[str, ...] = (),
) -> AuditEvent:
    """Build the ``slack.decision`` event for one request."""
    counted = (
        decision.action.is_ask
        and decision.allowed
        and decision.outcome
        in (Outcome.ALLOWED, Outcome.ALLOWED_PARTIAL, Outcome.MODEL_ONLY)
    )
    event_id = (
        new_id()
        if decision.allowed
        else _merge_id(DECISION_ACTION, decision, context, now)
    )
    return _event(
        event_id=event_id,
        action=DECISION_ACTION,
        target_type=ASK_COUNTED_TARGET if counted else DECISION_TARGET,
        decision=decision,
        context=context,
        now=now,
        metadata=decision_metadata(
            decision, context, planner=planner, considered=considered
        ),
    )


def build_execute_event(
    decision: Decision, context: DecisionContext, now: datetime
) -> AuditEvent:
    """Build ``slack.execute.allowed|blocked`` (the recent-attempts source)."""
    action = EXECUTE_ALLOWED_ACTION if decision.allowed else EXECUTE_BLOCKED_ACTION
    metadata = {
        "action": decision.action.value,
        "outcome": decision.outcome.value,
        "reason": decision.reason.value if decision.reason else None,
        "slack_user_id": context.slack_user_id,
        "channel_id": context.channel_id,
        "principal_id": context.principal_id,
        "settings_version": decision.settings_version,
        "repeat_count": 1,
    }
    event_id = (
        new_id() if decision.allowed else _merge_id(action, decision, context, now)
    )
    return _event(
        event_id=event_id,
        action=action,
        target_type=EXECUTE_TARGET,
        decision=decision,
        context=context,
        now=now,
        metadata=metadata,
    )


class DecisionRecorder:
    """Write decision events; blocked repeats merge into a counter."""

    def __init__(self, audit_events: Repository[AuditEvent]) -> None:
        """Bind the tenant-scoped audit repository."""
        self._audit = audit_events

    def record(
        self,
        decision: Decision,
        context: DecisionContext,
        now: datetime,
        *,
        planner: PlannerRecord | None = None,
        considered: tuple[str, ...] = (),
    ) -> AuditEvent:
        """Record the request's decision (and its Execute attempt record)."""
        main = build_decision_event(
            decision, context, now, planner=planner, considered=considered
        )
        stored = self._write(main, merge=not decision.allowed)
        if decision.action.is_execute:
            self._write(
                build_execute_event(decision, context, now), merge=not decision.allowed
            )
        return stored

    def _write(self, event: AuditEvent, *, merge: bool) -> AuditEvent:
        if not merge:
            return self._audit.create(event)
        try:
            current = self._audit.get_scoped(event.audit_event_id, event.org_id)
        except KeyError:
            try:
                return self._audit.create(event)
            except ValueError:  # a concurrent writer created it first
                current = self._audit.get_scoped(event.audit_event_id, event.org_id)
        payload = json.loads(current.model_dump_json())
        payload["metadata"]["repeat_count"] = (
            int(payload["metadata"].get("repeat_count", 1)) + 1
        )
        payload["metadata"]["last_at"] = event.created_at.isoformat()
        return self._audit.put(AuditEvent.model_validate_json(json.dumps(payload)))
