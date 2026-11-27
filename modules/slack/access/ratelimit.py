"""Ask rate limits derived from stored decisions (no counters to leak or reset).

Open Ask spends the organization's own AI key, so each Slack user, channel and
the organization are limited over a rolling window. The counts are the
organization's own counted ``slack.decision`` audit events (allowed Ask
requests), read with the audit paging index; nothing else is stored, so a
restart, a purge or a retention sweep can never leave a stale counter behind.
The evaluation itself is pure: it takes the timestamps and the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil

from application.ports.repository import Repository
from contracts.v1 import AuditEvent
from contracts.v1.slack_access import AskLimits
from modules.slack.access.decision_audit import (
    ASK_COUNTED_TARGET,
    DECISION_ACTION,
)
from modules.slack.access.models import AskUsage, Reason

USER_WINDOW = timedelta(minutes=10)
CHANNEL_WINDOW = timedelta(minutes=10)
ORG_WINDOW = timedelta(days=1)
_PAGE = 500
_MAX_PAGES = 12  # org_per_day is capped at 5000, so a window never needs more


@dataclass(frozen=True)
class RateVerdict:
    """Whether the asker is over a limit and how long until they are not."""

    reason: Reason | None = None
    retry_after_seconds: int = 0

    @property
    def limited(self) -> bool:
        """True when a limit is exceeded."""
        return self.reason is not None


def wait_seconds(
    stamps: tuple[datetime, ...], limit: int, window: timedelta, now: datetime
) -> int | None:
    """Seconds until fewer than ``limit`` stamps remain in the window, or None."""
    live = sorted(s for s in stamps if now - s < window)
    if len(live) < limit:
        return None
    release = live[len(live) - limit] + window
    return max(1, ceil((release - now).total_seconds()))


def check_ask_limits(limits: AskLimits, usage: AskUsage, now: datetime) -> RateVerdict:
    """Evaluate the three Ask limits; the binding (longest) wait wins."""
    candidates = (
        (Reason.RATE_LIMIT_USER, usage.user, limits.user_per_10_min, USER_WINDOW),
        (
            Reason.RATE_LIMIT_CHANNEL,
            usage.channel,
            limits.channel_per_10_min,
            CHANNEL_WINDOW,
        ),
        (Reason.RATE_LIMIT_ORG, usage.org, limits.org_per_day, ORG_WINDOW),
    )
    best = RateVerdict()
    for reason, stamps, limit, window in candidates:
        wait = wait_seconds(stamps, limit, window, now)
        if wait is not None and wait > best.retry_after_seconds:
            best = RateVerdict(reason, wait)
    return best


class AuditAskUsageReader:
    """Read the counted Ask decisions of one organization from the audit trail."""

    def __init__(self, audit_events: Repository[AuditEvent]) -> None:
        """Bind the (tenant-scoped) audit repository."""
        self._audit = audit_events

    def load(
        self, org_id: str, slack_user_id: str, channel_id: str, now: datetime
    ) -> AskUsage:
        """Return the asker's, the channel's and the org's counted timestamps."""
        user: list[datetime] = []
        channel: list[datetime] = []
        org: list[datetime] = []
        cursor_at: datetime | None = None
        cursor_id: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._audit.list_audit_page(
                org_id,
                None,
                action=DECISION_ACTION,
                target_type=ASK_COUNTED_TARGET,
                target_id=None,
                actor_principal_id=None,
                created_after=now - ORG_WINDOW,
                created_before=None,
                cursor_created_at=cursor_at,
                cursor_event_id=cursor_id,
                limit=_PAGE,
            )
            for event in page:
                org.append(event.created_at)
                if event.created_at >= now - USER_WINDOW:
                    if event.metadata.get("slack_user_id") == slack_user_id:
                        user.append(event.created_at)
                    if event.metadata.get("channel_id") == channel_id:
                        channel.append(event.created_at)
            if len(page) < _PAGE:
                break
            cursor_at, cursor_id = page[-1].created_at, page[-1].audit_event_id
        return AskUsage(tuple(user), tuple(channel), tuple(org))
