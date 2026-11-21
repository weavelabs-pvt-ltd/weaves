"""Bounded purge of expired sign-in states and expired or revoked sessions."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import AccountOAuthState, AccountSession
from shared.clock import utc_now
from shared.request_context import system_scoped

PURGE_BATCH = 200
# Rows are kept this long past expiry or revocation, which also keeps the
# string-ordered timestamp query from ever selecting a row that is still live.
PURGE_GRACE = timedelta(hours=1)


class AccountCleanup:
    """Delete finished account rows a little at a time, never scanning."""

    def __init__(
        self,
        states: Repository[AccountOAuthState],
        sessions: Repository[AccountSession],
    ) -> None:
        """Bind the global sign-in state and session repositories."""
        self._states = states
        self._sessions = sessions

    @system_scoped("account")
    def purge(self, limit: int = PURGE_BATCH, now: datetime | None = None) -> int:
        """Delete at most ``limit`` finished rows and return how many went.

        Each step reads only rows already past their expiry or revocation,
        through an indexed timestamp query, so a call costs the same however
        many live rows exist and is safe to repeat or run concurrently.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        cutoff = (now or utc_now()) - PURGE_GRACE
        remaining = limit
        for repository, field, id_field in (
            (self._states, "expires_at", "id"),
            (self._sessions, "expires_at", "id"),
            (self._sessions, "revoked_at", "id"),
        ):
            if remaining < 1:
                break
            remaining -= _purge_before(repository, field, id_field, cutoff, remaining)
        return limit - remaining


def _purge_before(
    repository: Repository[Any],
    field: str,
    id_field: str,
    cutoff: datetime,
    limit: int,
) -> int:
    """Delete up to ``limit`` rows whose timestamp field is before the cutoff."""
    deleted = 0
    for record in repository.list_before(field, cutoff, limit=limit):
        if getattr(record, field) >= cutoff:
            continue  # re-check exactly; the storage order is textual
        try:
            repository.delete(getattr(record, id_field))
        except KeyError:
            continue  # another purge removed it first
        deleted += 1
    return deleted
