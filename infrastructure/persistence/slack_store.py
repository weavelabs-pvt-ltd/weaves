"""PostgreSQL Slack installations and event inbox (migration 32)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from application.ports.slack import (
    HELD_STATUSES,
    ClaimResult,
    InboxEvent,
    InstallationStatus,
    SlackInstallation,
)
from shared.request_context import effective_org_id, system_scope

_INSTALLATION_COLUMNS = (
    "installation_id, team_id, org_id, workspace_id, team_name, enterprise_id, "
    "app_id, bot_user_id, bot_secret_ref, scopes, installed_by_principal_id, "
    "installed_by_name, installed_by_slack_user, status, installed_at, "
    "updated_at, revoked_at"
)
_INBOX_COLUMNS = (
    "event_id, team_id, org_id, installation_id, event_type, state, drop_reason, "
    "attempts, request_id, channel_id, user_id, message_ts, thread_ts, text, "
    "reaction, received_at"
)
_FIELDS = tuple(name.strip() for name in _INSTALLATION_COLUMNS.split(","))
_INBOX_FIELDS = tuple(name.strip() for name in _INBOX_COLUMNS.split(","))
_INSERT_INSTALLATION = (
    f"INSERT INTO slack_installations ({_INSTALLATION_COLUMNS}) VALUES ("
    + ", ".join(["%s"] * len(_FIELDS))
    + ")"
)
_UPDATE_INSTALLATION = """UPDATE slack_installations SET workspace_id = %s,
    team_name = %s, enterprise_id = %s, app_id = %s, bot_user_id = %s,
    bot_secret_ref = %s, scopes = %s, installed_by_principal_id = %s,
    installed_by_name = %s, installed_by_slack_user = %s, status = 'active',
    installed_at = %s, updated_at = %s, revoked_at = NULL
    WHERE installation_id = %s"""
_INSERT_EVENT = (
    f"INSERT INTO slack_event_inbox ({_INBOX_COLUMNS}) VALUES ("
    + ", ".join(["%s"] * len(_INBOX_FIELDS))
    + ") ON CONFLICT DO NOTHING RETURNING 1"
)


def _installation(row: Any) -> SlackInstallation:
    data = dict(zip(_FIELDS, row, strict=True))
    data["scopes"] = tuple(data["scopes"] or ())
    return SlackInstallation(**data)


def _event(row: Any) -> InboxEvent:
    return InboxEvent(**dict(zip(_INBOX_FIELDS, row, strict=True)))


def _values(installation: SlackInstallation) -> tuple[Any, ...]:
    data = {name: getattr(installation, name) for name in _FIELDS}
    data["scopes"] = list(installation.scopes)
    return tuple(data[name] for name in _FIELDS)


class PostgresSlackStore:
    """Installations and inbox over the product store's scoped connections."""

    def __init__(self, store: Any) -> None:
        """Use the store for scoped connections and advisory locks."""
        self._store = store

    # -- installations ---------------------------------------------------

    def find_by_team(self, team_id: str) -> SlackInstallation | None:
        """The held installation of ``team_id`` (event ingest, purpose slack)."""
        with system_scope("slack"), self._store.connection() as connection:
            row = connection.execute(
                f"SELECT {_INSTALLATION_COLUMNS} FROM slack_installations "
                "WHERE team_id = %s AND status = ANY(%s)",
                (team_id, list(HELD_STATUSES)),
            ).fetchone()
        return _installation(row) if row else None

    def list_for_org(self) -> list[SlackInstallation]:
        """The current organization's installations, newest first."""
        with self._store.connection() as connection:
            rows = connection.execute(
                f"SELECT {_INSTALLATION_COLUMNS} FROM slack_installations "
                "WHERE org_id = %s ORDER BY updated_at DESC, installation_id",
                (effective_org_id(),),
            ).fetchall()
        return [_installation(row) for row in rows]

    def get_for_org(self, installation_id: str) -> SlackInstallation | None:
        """One installation of the current organization."""
        with self._store.connection() as connection:
            row = connection.execute(
                f"SELECT {_INSTALLATION_COLUMNS} FROM slack_installations "
                "WHERE installation_id = %s AND org_id = %s",
                (installation_id, effective_org_id()),
            ).fetchone()
        return _installation(row) if row else None

    def claim(self, candidate: SlackInstallation) -> ClaimResult:
        """Bind the workspace under an advisory lock; the index is the backstop."""
        with system_scope("slack"), self._store.transaction() as connection:
            self._store.acquire_advisory_transaction_lock(
                f"slack-team:{candidate.team_id}"
            )
            rows = [
                _installation(row)
                for row in connection.execute(
                    f"SELECT {_INSTALLATION_COLUMNS} FROM slack_installations "
                    "WHERE team_id = %s",
                    (candidate.team_id,),
                ).fetchall()
            ]
            holder = next((r for r in rows if r.status in HELD_STATUSES), None)
            if holder is not None and holder.org_id != candidate.org_id:
                return ClaimResult("conflict")
            mine = next((r for r in rows if r.org_id == candidate.org_id), None)
            if mine is None:
                connection.execute(_INSERT_INSTALLATION, _values(candidate))
                return ClaimResult("created", candidate)
            connection.execute(
                _UPDATE_INSTALLATION,
                (
                    candidate.workspace_id,
                    candidate.team_name,
                    candidate.enterprise_id,
                    candidate.app_id,
                    candidate.bot_user_id,
                    candidate.bot_secret_ref,
                    list(candidate.scopes),
                    candidate.installed_by_principal_id,
                    candidate.installed_by_name,
                    candidate.installed_by_slack_user,
                    candidate.installed_at,
                    candidate.updated_at,
                    mine.installation_id,
                ),
            )
            return ClaimResult(
                "updated",
                replace(candidate, installation_id=mine.installation_id),
                previous_secret_ref=mine.bot_secret_ref,
            )

    def set_status(
        self,
        installation_id: str,
        status: InstallationStatus,
        now: datetime,
        *,
        clear_secret: bool = False,
    ) -> SlackInstallation | None:
        """Change a status (any scope the caller holds: tenant or slack)."""
        revoked = None if status in HELD_STATUSES else now
        with self._store.connection() as connection:
            row = connection.execute(
                "UPDATE slack_installations SET status = %s, updated_at = %s, "
                "revoked_at = %s, bot_secret_ref = CASE WHEN %s THEN NULL "
                "ELSE bot_secret_ref END WHERE installation_id = %s "
                f"RETURNING {_INSTALLATION_COLUMNS}",
                (status, now, revoked, clear_secret, installation_id),
            ).fetchone()
        return _installation(row) if row else None

    # -- inbox ---------------------------------------------------------------

    def record_event(self, event: InboxEvent) -> bool:
        """Insert once; a repeated event id or message inserts nothing."""
        values = tuple(getattr(event, name) for name in _INBOX_FIELDS)
        with system_scope("slack"), self._store.connection() as connection:
            return connection.execute(_INSERT_EVENT, values).fetchone() is not None

    def list_events(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[InboxEvent]:
        """Inbox rows oldest first."""
        with system_scope("slack"), self._store.connection() as connection:
            rows = connection.execute(
                f"SELECT {_INBOX_COLUMNS} FROM slack_event_inbox "
                "WHERE (%s::text IS NULL OR state = %s) "
                "ORDER BY received_at, event_id LIMIT %s",
                (state, state, limit),
            ).fetchall()
        return [_event(row) for row in rows]

    def purge_events(
        self,
        before: datetime,
        *,
        limit: int | None = None,
        include_pending: bool = False,
    ) -> int:
        """Delete up to ``limit`` rows received before ``before`` (bounded)."""
        with system_scope("slack"), self._store.connection() as connection:
            return int(
                connection.execute(
                    "DELETE FROM slack_event_inbox WHERE event_id IN ("
                    "SELECT event_id FROM slack_event_inbox WHERE received_at < %s "
                    "AND (%s OR state <> 'pending') ORDER BY received_at LIMIT %s)",
                    (before, include_pending, limit if limit is not None else 2**31),
                ).rowcount
            )

    def drop_pending_for_team(self, team_id: str, reason: str, now: datetime) -> int:
        """Drop the team's pending rows and erase their text."""
        with system_scope("slack"), self._store.connection() as connection:
            return int(
                connection.execute(
                    "UPDATE slack_event_inbox SET state = 'dropped', "
                    "drop_reason = %s, text = NULL WHERE team_id = %s "
                    "AND state = 'pending'",
                    (reason, team_id),
                ).rowcount
            )

    # -- lifecycle (sweep and purge) ----------------------------------------

    def list_unfinished(self, limit: int = 100) -> list[SlackInstallation]:
        """Ended installations whose token reference was not cleared."""
        with system_scope("slack"), self._store.connection() as connection:
            rows = connection.execute(
                f"SELECT {_INSTALLATION_COLUMNS} FROM slack_installations "
                "WHERE status <> ALL(%s) AND bot_secret_ref IS NOT NULL "
                "ORDER BY updated_at LIMIT %s",
                (list(HELD_STATUSES), limit),
            ).fetchall()
        return [_installation(row) for row in rows]

    def delete_installation(self, installation_id: str) -> bool:
        """Delete one row of the current organization."""
        with self._store.connection() as connection:
            removed = connection.execute(
                "DELETE FROM slack_installations "
                "WHERE installation_id = %s AND org_id = %s",
                (installation_id, effective_org_id()),
            ).rowcount
        return bool(removed)

    def delete_org_events(self, org_id: str, limit: int) -> int:
        """Delete up to ``limit`` inbox rows of ``org_id``."""
        with system_scope("slack"), self._store.connection() as connection:
            return int(
                connection.execute(
                    "DELETE FROM slack_event_inbox WHERE event_id IN ("
                    "SELECT event_id FROM slack_event_inbox WHERE org_id = %s "
                    "LIMIT %s)",
                    (org_id, limit),
                ).rowcount
            )

    def count_for_org(self, org_id: str) -> int:
        """Rows ``org_id`` still owns in both Slack tables."""
        with system_scope("slack"), self._store.connection() as connection:
            row = connection.execute(
                "SELECT (SELECT count(*) FROM slack_installations "
                "WHERE org_id = %s) + (SELECT count(*) FROM slack_event_inbox "
                "WHERE org_id = %s)",
                (org_id, org_id),
            ).fetchone()
        return int(row[0])
