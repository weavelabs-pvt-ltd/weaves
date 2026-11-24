"""Persistence for platform controls, operator actions and quarantine records.

``PostgresControlsStore`` reads through ``store.connection()`` (so the API and
worker, which run as the restricted role, can read the switches) and writes
through ``store.transaction()`` (operator scripts, on the owner connection; the
restricted role has no write policy and no access to the audit tables).
``MemoryControlsStore`` backs development and unit tests with the same contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from threading import Lock
from typing import Any, Protocol

from modules.platform_controls.controls import SWITCHES, StoredControls


@dataclass(frozen=True)
class OperatorAction:
    """One operator command, as recorded in the append-only audit table."""

    action_id: str
    at: datetime
    operator: str
    command: str
    target: str
    org_id: str | None
    reason: str
    ticket: str | None
    correlation_id: str
    result: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Quarantine:
    """What one tenant quarantine changed, so release can undo exactly that."""

    org_id: str
    state: str
    changes: dict[str, Any]
    quarantined_by: str
    reason: str
    correlation_id: str
    quarantined_at: datetime
    released_by: str | None = None
    released_at: datetime | None = None
    release_reason: str | None = None


class ControlsStore(Protocol):
    """Storage the controls, audit and quarantine services need."""

    def load(self) -> StoredControls:
        """Read the single controls row (defaults when absent)."""
        ...

    def update(
        self,
        changes: dict[str, bool | None],
        *,
        message: str | None,
        keep_message: bool,
        operator: str,
        reason: str,
        action: OperatorAction | None,
    ) -> StoredControls:
        """Apply switch changes (None clears to default) and the audit row together."""
        ...

    def append_action(self, action: OperatorAction) -> None:
        """Append one operator action."""
        ...

    def list_actions(
        self, limit: int, org_id: str | None = None
    ) -> list[OperatorAction]:
        """Newest first."""
        ...

    def get_quarantine(self, org_id: str) -> Quarantine | None:
        """The organization's quarantine record, if any."""
        ...

    def put_quarantine(
        self, record: Quarantine, action: OperatorAction | None = None
    ) -> None:
        """Insert or replace the record (and append the audit row atomically)."""
        ...


def _validate(changes: dict[str, bool | None]) -> None:
    unknown = set(changes) - set(SWITCHES)
    if unknown or not changes:
        raise ValueError("choose at least one known switch: " + ", ".join(SWITCHES))


class MemoryControlsStore:
    """In-process implementation for development and tests."""

    def __init__(self) -> None:
        """Start with no overrides, no actions and no quarantines."""
        self._lock = Lock()
        self._controls = StoredControls()
        self._actions: list[OperatorAction] = []
        self._quarantines: dict[str, Quarantine] = {}

    def load(self) -> StoredControls:
        """Read the row."""
        with self._lock:
            return self._controls

    def update(
        self,
        changes: dict[str, bool | None],
        *,
        message: str | None,
        keep_message: bool,
        operator: str,
        reason: str,
        action: OperatorAction | None,
    ) -> StoredControls:
        """Apply the changes and append the audit row."""
        _validate(changes)
        with self._lock:
            current = self._controls
            self._controls = replace(
                current,
                **changes,
                version=current.version + 1,
                message=current.message if keep_message else message,
                updated_by=operator,
                reason=reason,
            )
            if action is not None:
                self._actions.append(action)
            return self._controls

    def append_action(self, action: OperatorAction) -> None:
        """Append."""
        with self._lock:
            self._actions.append(action)

    def list_actions(
        self, limit: int, org_id: str | None = None
    ) -> list[OperatorAction]:
        """Newest first."""
        with self._lock:
            rows = [a for a in self._actions if org_id in (None, a.org_id)]
        return sorted(rows, key=lambda a: a.at, reverse=True)[:limit]

    def get_quarantine(self, org_id: str) -> Quarantine | None:
        """Read."""
        with self._lock:
            return self._quarantines.get(org_id)

    def put_quarantine(
        self, record: Quarantine, action: OperatorAction | None = None
    ) -> None:
        """Write."""
        with self._lock:
            self._quarantines[record.org_id] = record
            if action is not None:
                self._actions.append(action)


_CONTROL_COLUMNS = (
    "version, admissions_paused, worker_paused, read_only, signup_enabled, "
    "message, updated_by, reason"
)
_ACTION_COLUMNS = (
    "action_id, at, operator, command, target, org_id, reason, ticket, "
    "correlation_id, result, details"
)


def _action_values(action: OperatorAction) -> tuple[Any, ...]:
    return (
        action.action_id,
        action.at,
        action.operator,
        action.command,
        action.target,
        action.org_id,
        action.reason,
        action.ticket,
        action.correlation_id,
        action.result,
        json.dumps(action.details, default=str, sort_keys=True),
    )


def _insert_action(connection: Any, action: OperatorAction) -> None:
    connection.execute(
        f"INSERT INTO operator_actions ({_ACTION_COLUMNS}) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
        _action_values(action),
    )


class PostgresControlsStore:
    """The three migration-30 tables over the shared connection pool."""

    def __init__(self, store: Any) -> None:
        """Bind the process's ``ProductPostgresStore``."""
        self._store = store

    def load(self) -> StoredControls:
        """Read the row; any role may (the select policy is unconditional)."""
        with self._store.connection() as connection:
            row = connection.execute(
                f"SELECT {_CONTROL_COLUMNS} FROM platform_controls WHERE id = 1"
            ).fetchone()
        return StoredControls() if row is None else StoredControls(*row)

    def update(
        self,
        changes: dict[str, bool | None],
        *,
        message: str | None,
        keep_message: bool,
        operator: str,
        reason: str,
        action: OperatorAction | None,
    ) -> StoredControls:
        """Update the row and append the audit row in one transaction."""
        _validate(changes)
        assignments = ", ".join(f"{name} = %s" for name in changes)
        params: list[Any] = list(changes.values())
        message_sql = "" if keep_message else ", message = %s"
        if not keep_message:
            params.append(message)
        with self._store.transaction() as connection:
            row = connection.execute(
                f"UPDATE platform_controls SET {assignments}{message_sql}, "
                "version = version + 1, updated_by = %s, reason = %s, "
                "updated_at = now() WHERE id = 1 "
                f"RETURNING {_CONTROL_COLUMNS}",
                (*params, operator, reason),
            ).fetchone()
            if row is None:
                raise RuntimeError("platform_controls row is missing")
            if action is not None:
                _insert_action(connection, action)
        return StoredControls(*row)

    def append_action(self, action: OperatorAction) -> None:
        """Append one row."""
        with self._store.transaction() as connection:
            _insert_action(connection, action)

    def list_actions(
        self, limit: int, org_id: str | None = None
    ) -> list[OperatorAction]:
        """Newest first."""
        where, params = (
            ("", (limit,))
            if org_id is None
            else (
                "WHERE org_id = %s",
                (org_id, limit),
            )
        )
        with self._store.connection() as connection:
            rows = connection.execute(
                f"SELECT {_ACTION_COLUMNS} FROM operator_actions {where} "
                "ORDER BY at DESC, action_id LIMIT %s",
                params,
            ).fetchall()
        return [OperatorAction(*row) for row in rows]

    def get_quarantine(self, org_id: str) -> Quarantine | None:
        """Read one record."""
        with self._store.connection() as connection:
            row = connection.execute(
                "SELECT org_id, state, changes, quarantined_by, reason, "
                "correlation_id, quarantined_at, released_by, released_at, "
                "release_reason FROM org_quarantines WHERE org_id = %s",
                (org_id,),
            ).fetchone()
        return None if row is None else Quarantine(*row)

    def put_quarantine(
        self, record: Quarantine, action: OperatorAction | None = None
    ) -> None:
        """Upsert the record and append the audit row in one transaction."""
        with self._store.transaction() as connection:
            connection.execute(
                "INSERT INTO org_quarantines (org_id, state, changes, "
                "quarantined_by, reason, correlation_id, quarantined_at, "
                "released_by, released_at, release_reason) "
                "VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (org_id) DO UPDATE SET state = EXCLUDED.state, "
                "changes = EXCLUDED.changes, "
                "quarantined_by = EXCLUDED.quarantined_by, "
                "reason = EXCLUDED.reason, "
                "correlation_id = EXCLUDED.correlation_id, "
                "quarantined_at = EXCLUDED.quarantined_at, "
                "released_by = EXCLUDED.released_by, "
                "released_at = EXCLUDED.released_at, "
                "release_reason = EXCLUDED.release_reason",
                (
                    record.org_id,
                    record.state,
                    json.dumps(record.changes, default=str, sort_keys=True),
                    record.quarantined_by,
                    record.reason,
                    record.correlation_id,
                    record.quarantined_at,
                    record.released_by,
                    record.released_at,
                    record.release_reason,
                ),
            )
            if action is not None:
                _insert_action(connection, action)
