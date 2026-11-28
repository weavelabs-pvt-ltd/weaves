"""Shared pieces of Stop and Try again: find the run, authorize the person, change it.

``PressContext`` is the same for a button press and for the ``@Weaves stop|retry``
text fallback; only a button carries a reference and a message ``ts`` to check.
A press is resolved to the thread row of *this* request, never trusted from the
payload alone: the reference must equal the row's random ``run_ref`` (compared in
constant time) and the message must be one of the row's own messages, so a forged
or stale button, or one copied from another thread, finds nothing.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime

from application.ports.repository import Repository
from application.ports.slack import SlackInstallation
from contracts.v1.slack_threads import SlackThread
from modules.slack.access.service import SlackAccessService
from modules.slack.dispatch.threads import ThreadBook


@dataclass(frozen=True)
class PressContext:
    """One press: where it came from, who pressed and how it must be traced."""

    channel_id: str
    thread_ts: str | None
    user_id: str
    source: str  # "button" or "text"
    request_id: str
    now: datetime
    ref: str | None = None
    message_ts: str | None = None


@dataclass(frozen=True)
class Verdict:
    """Whether the person may act on the run, why, and who they are mapped to."""

    allowed: bool
    reason: str
    principal_id: str | None = None


@dataclass(frozen=True)
class Outcome:
    """What a flow did: the audit facts and the private reply (if any)."""

    allowed: bool
    reason: str
    reply: str | None = None


class PressAuth:
    """Find the run a press is about and decide whether the presser may act."""

    def __init__(self, book: ThreadBook, access: SlackAccessService) -> None:
        """Bind the thread rows and the access service."""
        self._book = book
        self._access = access

    def resolve(
        self, installation: SlackInstallation, ctx: PressContext
    ) -> SlackThread | None:
        """The run this press is about, or ``None`` (no such run, stale, forged)."""
        if not ctx.thread_ts:
            return None
        row = self._book.find(
            installation.org_id, installation.team_id, ctx.channel_id, ctx.thread_ts
        )
        if row is None or ctx.source != "button":
            return row
        if not row.run_ref or ctx.ref is None:
            return None
        if not hmac.compare_digest(row.run_ref.encode(), ctx.ref.encode()):
            return None
        mine = {ts for ts in (row.status_ts, row.answer_ts) if ts}
        return row if ctx.message_ts in mine else None

    def authorize(self, row: SlackThread, ctx: PressContext) -> Verdict:
        """The requester, or a mapped active member holding ``slack.manage``."""
        principal, manages = self._access.presser_standing(ctx.user_id)
        if ctx.user_id == row.requester_slack_user_id:
            return Verdict(True, "requester", principal)
        if manages:
            return Verdict(True, "admin", principal)
        return Verdict(
            False, "not_mapped" if principal is None else "not_admin", principal
        )


class RowLock:
    """Compare-and-set a thread row under the same advisory lock the reply tick uses."""

    def __init__(
        self,
        threads: Repository[SlackThread],
        book: ThreadBook,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        lock: Callable[[str], None] | None,
    ) -> None:
        """Bind the rows, the transaction boundary and the advisory lock."""
        self._threads = threads
        self._book = book
        self._uow = unit_of_work
        self._lock = lock

    def change(
        self,
        row: SlackThread,
        allowed: Callable[[SlackThread], bool],
        change: Callable[[SlackThread], SlackThread],
        now: datetime,
    ) -> SlackThread | None:
        """Apply ``change`` to the stored row if ``allowed(stored)``; else ``None``."""
        with self._uow():
            if self._lock is not None:
                self._lock(f"weaves-slack-reply:{row.id}")
            fresh = self._threads.get_scoped(row.id, row.org_id)
            if not allowed(fresh):
                return None
            return self._book.save(change(fresh), now)

    def read(self, row: SlackThread) -> SlackThread | None:
        """The stored row now, or ``None`` if it is gone."""
        try:
            return self._threads.get_scoped(row.id, row.org_id)
        except KeyError:
            return None
