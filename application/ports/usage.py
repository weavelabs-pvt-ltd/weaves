"""Usage ledger port: idempotent reserve, settle, release and rollups.

One row per model call keyed by ``(org_id, reservation_key)``; the key is the
agent run id, and each agent run makes at most one provider request. Every
mutation is safe to repeat: a replayed reserve returns the existing row and a
replayed settle changes nothing, so tokens are never counted twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from contracts.v1 import (
    UsageEvent,
    UsageGroup,
    UsageGroupBy,
    UsageState,
    UsageTotals,
)


@dataclass(frozen=True)
class UsageReservationRequest:
    """What a model call intends to spend, recorded before the request."""

    org_id: str
    workspace_id: str
    reservation_key: str
    agent_run_id: str
    run_id: str
    agent_id: str
    provider_id: str | None
    model_id: str | None
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_nano_usd: int | None
    now: datetime


@dataclass(frozen=True)
class UsageAdmission:
    """Caps a new reservation must fit under (settled + outstanding + new)."""

    day_start: datetime
    month_start: datetime
    tokens_per_day: int
    tokens_per_month: int
    cost_nano_usd_per_day: int | None
    # When False a breach is reported but the reservation is still recorded.
    enforce: bool


@dataclass(frozen=True)
class UsageReservationResult:
    """The reservation row, whether this call created it, and any cap breach."""

    event: UsageEvent | None
    created: bool
    breach: str | None = None


@dataclass(frozen=True)
class UsageSettlement:
    """Provider-reported usage for one call (idempotent upsert)."""

    org_id: str
    workspace_id: str
    reservation_key: str
    agent_run_id: str
    run_id: str
    agent_id: str
    provider_id: str | None
    model_id: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_nano_usd: int | None
    outcome: str
    now: datetime


@dataclass(frozen=True)
class OrgUsageRow:
    """One operator report row: an organization's settled usage for a period."""

    org_id: str
    org_name: str | None
    period: str
    totals: UsageTotals
    unknown_calls: int


class UsageLedger(Protocol):
    """Durable per-organization usage records (PostgreSQL or in memory)."""

    def reserve(
        self, request: UsageReservationRequest, admission: UsageAdmission | None
    ) -> UsageReservationResult:
        """Record a reservation once; a repeat returns the existing row."""
        ...

    def settle(self, settlement: UsageSettlement) -> bool:
        """Record real usage; True only when this call newly settled the row."""
        ...

    def release(
        self, org_id: str, reservation_key: str, *, outcome: str, now: datetime
    ) -> bool:
        """Mark a reservation that never reached the provider as released."""
        ...

    def mark_unknown(
        self, org_id: str, reservation_key: str, *, outcome: str, now: datetime
    ) -> bool:
        """Mark a reservation whose request may have reached the provider."""
        ...

    def get(self, org_id: str, reservation_key: str) -> UsageEvent | None:
        """Return one ledger row."""
        ...

    def list_reserved_before(
        self, before: datetime, *, limit: int
    ) -> tuple[UsageEvent, ...]:
        """Reservations older than ``before`` across organizations (system)."""
        ...

    def summary(
        self, org_id: str, start: date, end: date, group_by: UsageGroupBy
    ) -> tuple[UsageTotals, tuple[UsageGroup, ...], int, int]:
        """Totals, groups, unknown and in-flight call counts for an inclusive range."""
        ...

    def daily_rows(self, org_id: str) -> list[dict[str, object]]:
        """Every rollup row of one organization, for the tenant export."""
        ...

    def count_state(self, org_id: str, state: UsageState) -> int:
        """Number of ledger rows in a state."""
        ...

    def purge_organization(self, limit: int) -> int:
        """Delete up to ``limit`` rows the current organization owns."""
        ...

    def remaining_rows(self) -> int:
        """Ledger and rollup rows the current organization still owns."""
        ...

    def purge_before(self, day: date, limit: int) -> int:
        """Retention: delete finished rows and rollups before ``day`` (system)."""
        ...

    def purge_deleted_organizations(self, limit: int) -> int:
        """Drop rows a late settlement wrote for an already deleted organization."""
        ...

    def operator_report(
        self, start: date, end: date, *, by: str, org_id: str | None
    ) -> list[OrgUsageRow]:
        """Per-organization rollups by ``day`` or ``month`` (system)."""
        ...
