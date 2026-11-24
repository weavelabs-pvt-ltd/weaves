"""Organization-scoped usage report (Owner and Admin only)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from application.ports.usage import UsageLedger
from contracts.v1 import Permission, UsageGroupBy, UsageSummary
from shared.clock import utc_now
from shared.request_context import effective_org_id, effective_principal_id

MAX_RANGE_DAYS = 92
DEFAULT_RANGE_DAYS = 30


class UsageQueries:
    """Read the caller's own organization's usage rollups."""

    def __init__(
        self, ledger: UsageLedger, authorize: Callable[[str, Permission], None]
    ) -> None:
        """Bind the ledger and the organization-administration permission check."""
        self.ledger = ledger
        self._authorize = authorize

    def summary(
        self,
        start: date | None,
        end: date | None,
        group_by: UsageGroupBy,
    ) -> UsageSummary:
        """Settled usage per agent, model or day for an inclusive UTC range."""
        self._authorize(
            effective_principal_id("local-developer"), Permission.ORGANIZATIONS_ADMIN
        )
        last = end or utc_now().date()
        first = start or last - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        if first > last:
            raise ValueError("from must not be after to")
        if (last - first).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"The range cannot exceed {MAX_RANGE_DAYS} days")
        totals, groups, unknown, in_flight = self.ledger.summary(
            effective_org_id(), first, last, group_by
        )
        return UsageSummary(
            from_date=first,
            to_date=last,
            group_by=group_by,
            totals=totals,
            groups=groups,
            unknown_calls=unknown,
            in_flight_calls=in_flight,
        )
