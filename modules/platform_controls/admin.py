"""Operator commands that change the platform-wide switches, with audit."""

from __future__ import annotations

from typing import Any

from modules.platform_controls.audit import OK, OperatorAudit, clean
from modules.platform_controls.controls import (
    SWITCHES,
    PlatformControls,
    StoredControls,
)
from modules.platform_controls.store import ControlsStore


def view(stored: StoredControls, controls: PlatformControls | None) -> dict[str, Any]:
    """Stored overrides next to the effective values this process would use."""
    effective = controls.snapshot() if controls is not None else None
    return {
        "version": stored.version,
        "overrides": {name: getattr(stored, name) for name in SWITCHES},
        "effective": None
        if effective is None
        else {name: getattr(effective, name) for name in SWITCHES},
        "message": stored.message,
        "updated_by": stored.updated_by,
        "reason": stored.reason,
        "note": "NULL/None override means the environment default applies; "
        "processes pick up a change within about 5 seconds",
    }


class ControlsAdmin:
    """Set and clear switches; each change and its audit row commit together."""

    def __init__(
        self,
        store: ControlsStore,
        audit: OperatorAudit,
        controls: PlatformControls | None = None,
    ) -> None:
        """Bind storage, the audit helper and (optionally) this process's reader."""
        self._store = store
        self._audit = audit
        self._controls = controls

    def status(self) -> dict[str, Any]:
        """The stored row."""
        if self._controls is not None:
            self._controls.forget()
        return view(self._store.load(), self._controls)

    def set(
        self,
        operator: str,
        reason: str,
        values: dict[str, bool],
        *,
        message: str | None = None,
        ticket: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Force switches to explicit values, optionally with a public message."""
        return self._change(
            operator,
            reason,
            "platform.set",
            dict(values),
            message,
            keep_message=message is None,
            ticket=ticket,
            correlation_id=correlation_id,
        )

    def clear(
        self,
        operator: str,
        reason: str,
        names: tuple[str, ...],
        *,
        clear_message: bool = False,
        ticket: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Return switches to their environment defaults."""
        changes: dict[str, bool | None] = {name: None for name in names or SWITCHES}
        return self._change(
            operator,
            reason,
            "platform.clear",
            changes,
            None,
            keep_message=not (clear_message or not names),
            ticket=ticket,
            correlation_id=correlation_id,
        )

    def _change(
        self,
        operator: str,
        reason: str,
        command: str,
        changes: dict[str, bool | None],
        message: str | None,
        *,
        keep_message: bool,
        ticket: str | None,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        who = clean(operator, "operator", 320)
        why = clean(reason, "reason", 500)
        text = clean(message, "message", 300) if message else None
        unknown = set(changes) - set(SWITCHES)
        if unknown or not changes:
            raise ValueError("choose at least one known switch: " + ", ".join(SWITCHES))
        before = self._store.load()
        action = self._audit.build(
            operator=who,
            command=command,
            target="platform_controls",
            reason=why,
            ticket=ticket,
            correlation_id=correlation_id,
            result=OK,
            details={
                "changes": {name: value for name, value in changes.items()},
                "before": {name: getattr(before, name) for name in SWITCHES},
                "message_set": text is not None,
            },
        )
        stored = self._store.update(
            changes,
            message=text,
            keep_message=keep_message,
            operator=who,
            reason=why,
            action=action,
        )
        if self._controls is not None:
            self._controls.forget()
        return view(stored, self._controls)
