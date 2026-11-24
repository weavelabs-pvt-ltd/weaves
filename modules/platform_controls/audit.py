"""The shared operator audit helper: who did what, when, why, under which id.

Every operator script records one ``operator_actions`` row per mutating command
(and per refused attempt) through ``OperatorAudit``. Rows are append-only (the
table rejects update, delete and truncate) and carry identifiers and counts,
never secrets, tokens or tenant content. ``correlation_id`` ties the row to the
script's structured log lines.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from modules.platform_controls.store import ControlsStore, OperatorAction
from shared.clock import new_id, utc_now

OK = "ok"
REFUSED = "refused"
MAX_REASON = 500
MAX_OPERATOR = 320


def new_correlation_id() -> str:
    """An opaque id for one operator command."""
    return "op-" + new_id()


def clean(value: str, name: str, limit: int) -> str:
    """Printable, non-empty, bounded text."""
    text = value.strip()
    if not text or len(text) > limit or not text.isprintable():
        raise ValueError(f"{name} must be printable text up to {limit} characters")
    return text


class OperatorAudit:
    """Build and append operator action rows."""

    def __init__(
        self, store: ControlsStore, clock: Callable[[], datetime] = utc_now
    ) -> None:
        """Bind the storage and the clock."""
        self._store = store
        self._clock = clock

    def build(
        self,
        *,
        operator: str,
        command: str,
        reason: str,
        target: str = "",
        org_id: str | None = None,
        ticket: str | None = None,
        correlation_id: str | None = None,
        result: str = OK,
        details: dict[str, Any] | None = None,
    ) -> OperatorAction:
        """Validate the fields and make one (not yet stored) action."""
        return OperatorAction(
            action_id=new_id(),
            at=self._clock(),
            operator=clean(operator, "operator", MAX_OPERATOR),
            command=clean(command, "command", 120),
            target=target[:300],
            org_id=org_id,
            reason=clean(reason, "reason", MAX_REASON),
            ticket=clean(ticket, "ticket", 120) if ticket else None,
            correlation_id=correlation_id or new_correlation_id(),
            result=clean(result, "result", 200),
            details=dict(details or {}),
        )

    def record(self, **fields: Any) -> OperatorAction:
        """Build and append one action."""
        action = self.build(**fields)
        self._store.append_action(action)
        return action

    def recent(
        self, limit: int = 50, org_id: str | None = None
    ) -> list[OperatorAction]:
        """The newest actions, optionally for one organization."""
        return self._store.list_actions(max(1, min(limit, 500)), org_id)
