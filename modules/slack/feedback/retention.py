"""The ``slack_feedback_retention`` worker tick: ratings expire after 400 days.

Design 3.13: feedback is kept ``WEAVES_USAGE_RETENTION_DAYS`` (default 400) days
after the reaction, like the usage ledger it is read next to, and is removed
sooner only with its organization (``PURGE_ORDER``). The delete follows the S7
inbox pattern: oldest first, ``BATCH`` rows per statement and at most
``MAX_BATCHES`` statements per tick, so a backlog drains over several ticks
without long transactions. It runs for every organization, also one whose
workspace was disconnected, and is not held by the Slack kill switch: deleting
expired data is not Slack activity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from application.ports.slack_feedback import FeedbackAdmin
from shared.clock import utc_now
from shared.metrics import SLACK_RETENTION

logger = logging.getLogger("product.slack")
BATCH = 500
MAX_BATCHES = 10


class FeedbackRetention:
    """Delete expired feedback in bounded batches."""

    def __init__(
        self,
        admin: FeedbackAdmin,
        *,
        days: int,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the system-purpose admin store and the retention period."""
        self._admin = admin
        self._days = days
        self._clock = clock

    def run(self) -> int:
        """Delete up to ``BATCH * MAX_BATCHES`` expired rows; returns the count."""
        cutoff = self._clock() - timedelta(days=self._days)
        removed = 0
        for _ in range(MAX_BATCHES):
            count = self._admin.expire(cutoff, BATCH)
            removed += count
            if count < BATCH:
                break
        if removed:
            SLACK_RETENTION.inc(removed, kind="feedback")
            logger.info("slack feedback retention removed=%d", removed)
        return removed
