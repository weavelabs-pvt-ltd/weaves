"""The ``slack.interaction`` audit record: one per press, never any message text.

Every Stop or Try again press (a button or the ``@Weaves stop|retry`` text
fallback) writes one event with the action, the outcome (``allowed`` or
``blocked``), a reason code, the Slack user, the mapped principal if there is one,
the channel, a short digest of the run reference (never the reference itself) and
the request id. Nothing a person wrote is stored.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import AuditEvent, AuditTargetScope
from shared.clock import new_id

INTERACTION_ACTION = "slack.interaction"
INTERACTION_TARGET = "slack_interaction"


def run_digest(run_ref: str) -> str:
    """A twelve character digest that links an audit event to a run, not to a button."""
    return hashlib.sha256(run_ref.encode()).hexdigest()[:12] if run_ref else ""


class InteractionAudit:
    """Write one audit event per press."""

    def __init__(self, audit_events: Repository[AuditEvent]) -> None:
        """Bind the tenant-scoped audit repository."""
        self._audit = audit_events

    def record(
        self,
        *,
        org_id: str,
        action: str,
        source: str,
        allowed: bool,
        reason: str,
        slack_user_id: str,
        principal_id: str | None,
        channel_id: str,
        run_ref: str,
        request_id: str,
        now: datetime,
    ) -> None:
        """Append the event for one press."""
        outcome = "allowed" if allowed else "blocked"
        metadata: dict[str, Any] = {
            "action": action,
            "source": source,
            "outcome": outcome,
            "reason": reason,
            "slack_user_id": slack_user_id,
            "principal_id": principal_id,
            "channel_id": channel_id,
            "run": run_digest(run_ref),
        }
        self._audit.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=principal_id or f"slack:{slack_user_id}",
                action=INTERACTION_ACTION,
                target_type=INTERACTION_TARGET,
                target_id=request_id,
                summary=f"Slack {action} {outcome}",
                request_id=request_id,
                metadata=metadata,
                created_at=now,
            )
        )
