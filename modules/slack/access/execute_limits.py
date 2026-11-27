"""Execute rate limits derived from stored decisions (design 3.8).

Execute actions persist work and spend the organization's AI key, so they have
their own rolling windows per Slack user and per channel, separate from Ask.
Like the Ask limits there are no counters: the count is the organization's own
``slack.execute.allowed`` audit events (one per allowed request, written by the
dispatcher under an id derived from the Slack event, so a replay adds none).
The request being decided is excluded by its event id, so a replay never counts
against itself.
"""

from __future__ import annotations

from datetime import datetime

from application.ports.repository import Repository
from contracts.v1 import AuditEvent
from contracts.v1.slack_access import ExecuteLimits
from modules.slack.access.decision_audit import (
    EXECUTE_ALLOWED_ACTION,
    EXECUTE_TARGET,
)
from modules.slack.access.models import ExecuteUsage, Reason
from modules.slack.access.ratelimit import (
    CHANNEL_WINDOW,
    USER_WINDOW,
    RateVerdict,
    wait_seconds,
)

_PAGE = 500
_MAX_PAGES = 4  # the windows hold a few dozen events; this is a hard bound


def check_execute_limits(
    limits: ExecuteLimits, usage: ExecuteUsage, now: datetime
) -> RateVerdict:
    """Evaluate the two Execute limits; the longer wait wins."""
    best = RateVerdict()
    candidates = (
        (Reason.RATE_LIMIT_USER, usage.user, limits.user_per_10_min, USER_WINDOW),
        (
            Reason.RATE_LIMIT_CHANNEL,
            usage.channel,
            limits.channel_per_10_min,
            CHANNEL_WINDOW,
        ),
    )
    for reason, stamps, limit, window in candidates:
        wait = wait_seconds(stamps, limit, window, now)
        if wait is not None and wait > best.retry_after_seconds:
            best = RateVerdict(reason, wait)
    return best


class AuditExecuteUsageReader:
    """Read the counted Execute decisions of one organization from the audit trail."""

    def __init__(self, audit_events: Repository[AuditEvent]) -> None:
        """Bind the (tenant-scoped) audit repository."""
        self._audit = audit_events

    def load(
        self,
        org_id: str,
        slack_user_id: str,
        channel_id: str,
        now: datetime,
        *,
        exclude_event_id: str | None = None,
    ) -> ExecuteUsage:
        """The asker's and the channel's allowed Execute timestamps (10 minutes)."""
        user: list[datetime] = []
        channel: list[datetime] = []
        cursor_at: datetime | None = None
        cursor_id: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._audit.list_audit_page(
                org_id,
                None,
                action=EXECUTE_ALLOWED_ACTION,
                target_type=EXECUTE_TARGET,
                target_id=None,
                actor_principal_id=None,
                created_after=now - USER_WINDOW,
                created_before=None,
                cursor_created_at=cursor_at,
                cursor_event_id=cursor_id,
                limit=_PAGE,
            )
            for event in page:
                meta = event.metadata
                if exclude_event_id and meta.get("event_id") == exclude_event_id:
                    continue
                if meta.get("slack_user_id") == slack_user_id:
                    user.append(event.created_at)
                if meta.get("channel_id") == channel_id:
                    channel.append(event.created_at)
            if len(page) < _PAGE:
                break
            cursor_at, cursor_id = page[-1].created_at, page[-1].audit_event_id
        return ExecuteUsage(tuple(user), tuple(channel))
