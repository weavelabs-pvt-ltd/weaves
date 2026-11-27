"""Process-local Slack store for development and unit tests (same semantics)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock

from application.ports.slack import (
    HELD_STATUSES,
    ClaimResult,
    InboxEvent,
    InstallationStatus,
    SlackInstallation,
)
from shared.request_context import effective_org_id


class InMemorySlackStore:
    """Every method is atomic under one lock."""

    def __init__(self) -> None:
        """Start empty."""
        self._lock = RLock()
        self._installations: dict[str, SlackInstallation] = {}
        self._events: dict[str, InboxEvent] = {}

    def find_by_team(self, team_id: str) -> SlackInstallation | None:
        """The held installation of ``team_id``."""
        with self._lock:
            return next(
                (
                    row
                    for row in self._installations.values()
                    if row.team_id == team_id and row.status in HELD_STATUSES
                ),
                None,
            )

    def list_held(self, limit: int = 500) -> list[SlackInstallation]:
        """Held installations of every organization (the health tick)."""
        with self._lock:
            rows = [
                r for r in self._installations.values() if r.status in HELD_STATUSES
            ]
        return sorted(rows, key=lambda r: r.installation_id)[:limit]

    def list_for_org(self) -> list[SlackInstallation]:
        """The current organization's installations, newest first."""
        org_id = effective_org_id()
        with self._lock:
            rows = [r for r in self._installations.values() if r.org_id == org_id]
        return sorted(rows, key=lambda r: r.updated_at, reverse=True)

    def get_for_org(self, installation_id: str) -> SlackInstallation | None:
        """One installation of the current organization."""
        with self._lock:
            row = self._installations.get(installation_id)
        return row if row is not None and row.org_id == effective_org_id() else None

    def claim(self, candidate: SlackInstallation) -> ClaimResult:
        """Bind the workspace; refuse when another organization holds it."""
        with self._lock:
            rows = [
                r
                for r in self._installations.values()
                if r.team_id == candidate.team_id
            ]
            holder = next((r for r in rows if r.status in HELD_STATUSES), None)
            if holder is not None and holder.org_id != candidate.org_id:
                return ClaimResult("conflict")
            mine = next((r for r in rows if r.org_id == candidate.org_id), None)
            if mine is None:
                self._installations[candidate.installation_id] = candidate
                return ClaimResult("created", candidate)
            updated = replace(candidate, installation_id=mine.installation_id)
            self._installations[mine.installation_id] = updated
            return ClaimResult("updated", updated, mine.bot_secret_ref)

    def set_status(
        self,
        installation_id: str,
        status: InstallationStatus,
        now: datetime,
        *,
        clear_secret: bool = False,
    ) -> SlackInstallation | None:
        """Change a status, optionally dropping the token reference."""
        with self._lock:
            row = self._installations.get(installation_id)
            if row is None:
                return None
            updated = replace(
                row,
                status=status,
                updated_at=now,
                revoked_at=None if status in HELD_STATUSES else now,
                bot_secret_ref=None if clear_secret else row.bot_secret_ref,
            )
            self._installations[installation_id] = updated
            return updated

    def record_event(self, event: InboxEvent) -> bool:
        """Insert once; a repeated event id or app mention inserts nothing."""
        with self._lock:
            if event.event_id in self._events:
                return False
            if event.event_type == "app_mention" and any(
                e.event_type == "app_mention"
                and (e.team_id, e.channel_id, e.message_ts)
                == (event.team_id, event.channel_id, event.message_ts)
                for e in self._events.values()
            ):
                return False
            self._events[event.event_id] = event
            return True

    def list_events(
        self, *, state: str | None = None, limit: int = 100
    ) -> list[InboxEvent]:
        """Inbox rows oldest first."""
        with self._lock:
            rows = [e for e in self._events.values() if state in (None, e.state)]
        return sorted(rows, key=lambda e: (e.received_at, e.event_id))[:limit]

    def purge_events(
        self,
        before: datetime,
        *,
        limit: int | None = None,
        include_pending: bool = False,
    ) -> int:
        """Delete up to ``limit`` rows received before ``before``."""
        with self._lock:
            stale = sorted(
                (
                    e
                    for e in self._events.values()
                    if e.received_at < before
                    and (include_pending or e.state != "pending")
                ),
                key=lambda e: e.received_at,
            )[:limit]
            for event in stale:
                del self._events[event.event_id]
        return len(stale)

    def drop_pending_for_team(self, team_id: str, reason: str, now: datetime) -> int:
        """Drop the team's pending rows and erase their text."""
        with self._lock:
            keys = [
                key
                for key, e in self._events.items()
                if e.team_id == team_id and e.state == "pending"
            ]
            for key in keys:
                self._events[key] = replace(
                    self._events[key], state="dropped", drop_reason=reason, text=None
                )
        return len(keys)

    def list_unfinished(self, limit: int = 100) -> list[SlackInstallation]:
        """Ended installations whose token reference was not cleared."""
        with self._lock:
            rows = [
                r
                for r in self._installations.values()
                if r.status not in HELD_STATUSES and r.bot_secret_ref is not None
            ]
        return sorted(rows, key=lambda r: r.updated_at)[:limit]

    def delete_installation(self, installation_id: str) -> bool:
        """Delete one row of the current organization."""
        with self._lock:
            row = self._installations.get(installation_id)
            if row is None or row.org_id != effective_org_id():
                return False
            del self._installations[installation_id]
        return True

    def delete_org_events(self, org_id: str, limit: int) -> int:
        """Delete up to ``limit`` inbox rows of ``org_id``."""
        with self._lock:
            keys = [k for k, e in self._events.items() if e.org_id == org_id][:limit]
            for key in keys:
                del self._events[key]
        return len(keys)

    def count_for_org(self, org_id: str) -> int:
        """Rows ``org_id`` still owns in both Slack structures."""
        with self._lock:
            return sum(r.org_id == org_id for r in self._installations.values()) + sum(
                e.org_id == org_id for e in self._events.values()
            )
