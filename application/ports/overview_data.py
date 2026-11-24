"""Aggregate Home metrics computed where the data lives (never client side)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

# Days of per-day history returned: the longest chart range (91 days) plus an
# equal prior period for the period-over-period comparison.
DAILY_HISTORY_DAYS = 182


class OverviewData(Protocol):
    """Bounded aggregate reads over one workspace, scoped to one caller."""

    def snapshot(
        self,
        org_id: str,
        workspace_id: str,
        principal_id: str,
        *,
        now: datetime,
        tz_offset_minutes: int,
    ) -> dict[str, Any]:
        """Return task, turn, daily-run and first-run aggregates.

        Private tasks of other principals never contribute. ``tz_offset_minutes``
        shifts day boundaries to the caller's local time.
        """
        ...
