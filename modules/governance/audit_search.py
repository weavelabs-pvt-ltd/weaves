"""Searchable audit history: the read side of the audit trail."""

from __future__ import annotations

import base64
import json
from datetime import datetime

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import AuditEvent, Permission
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class GovernanceAuditMixin(RuntimeCapabilityHost):
    """Own audit-search operations over the recorded audit stream."""

    def search_audit_events(
        self,
        *,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        actor_principal_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[AuditEvent, ...]:
        """Search the selected workspace's audit stream with keyset pagination."""
        self.authorize(effective_principal_id("local-developer"), Permission.AUDIT_READ)
        if not 1 <= limit <= 1001:
            raise ValueError("limit must be between 1 and 1001")
        if created_after is not None and created_after.utcoffset() is None:
            raise ValueError("created_after must include a timezone")
        if created_before is not None and created_before.utcoffset() is None:
            raise ValueError("created_before must include a timezone")
        if (
            created_after is not None
            and created_before is not None
            and created_after > created_before
        ):
            raise ValueError("created_after must not be later than created_before")
        cursor_position: tuple[datetime, str] | None = None
        if cursor is not None:
            if not cursor or len(cursor) > 1024:
                raise ValueError("audit cursor is invalid")
            try:
                encoded = cursor + "=" * (-len(cursor) % 4)
                cursor_payload = json.loads(
                    base64.b64decode(encoded, altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(cursor_payload, dict)
                    or set(cursor_payload) != {"created_at", "event_id"}
                    or not isinstance(cursor_payload["created_at"], str)
                    or not isinstance(cursor_payload["event_id"], str)
                    or not cursor_payload["event_id"]
                ):
                    raise ValueError
                cursor_created_at = datetime.fromisoformat(
                    cursor_payload["created_at"].replace("Z", "+00:00")
                )
                if cursor_created_at.utcoffset() is None:
                    raise ValueError
                cursor_position = (
                    cursor_created_at,
                    cursor_payload["event_id"],
                )
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError("audit cursor is invalid") from exc
        return self.audit_events.list_audit_page(
            effective_org_id(),
            effective_workspace_id(),
            action=action,
            target_type=target_type,
            target_id=target_id,
            actor_principal_id=actor_principal_id,
            created_after=created_after,
            created_before=created_before,
            cursor_created_at=(cursor_position[0] if cursor_position else None),
            cursor_event_id=(cursor_position[1] if cursor_position else None),
            limit=limit,
        )

    @staticmethod
    def audit_event_cursor(event: AuditEvent) -> str:
        """Return an opaque continuation token for an audit event page."""
        payload = json.dumps(
            {
                "created_at": event.created_at.isoformat(),
                "event_id": event.audit_event_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
