"""In-memory usage ledger for development and unit tests (same semantics)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from threading import RLock

from application.ports.usage import (
    OrgUsageRow,
    UsageAdmission,
    UsageReservationRequest,
    UsageReservationResult,
    UsageSettlement,
)
from application.ports.usage_caps import Committed, breached_cap
from contracts.v1 import (
    UsageEvent,
    UsageGroup,
    UsageGroupBy,
    UsageState,
    UsageTotals,
)
from infrastructure.persistence.usage_ledger import sum_totals
from shared.request_context import effective_org_id

_RollupKey = tuple[str, date, str, str, str]


def _day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


class InMemoryUsageLedger:
    """Process-local ledger; every method is atomic under one lock."""

    def __init__(self) -> None:
        """Start empty."""
        self._lock = RLock()
        self._rows: dict[tuple[str, str], UsageEvent] = {}
        # (org, day, agent, provider, model) -> [calls, in, out, cost, unpriced]
        self._daily: dict[_RollupKey, list[int]] = defaultdict(lambda: [0] * 5)

    def reserve(
        self, request: UsageReservationRequest, admission: UsageAdmission | None
    ) -> UsageReservationResult:
        """Record once; a replay returns the existing row."""
        key = (request.org_id, request.reservation_key)
        with self._lock:
            existing = self._rows.get(key)
            if existing is not None:
                return UsageReservationResult(existing, False)
            breach = self._breach(request, admission) if admission else None
            if breach is not None and admission is not None and admission.enforce:
                return UsageReservationResult(None, False, breach)
            event = UsageEvent(
                org_id=request.org_id,
                workspace_id=request.workspace_id,
                reservation_key=request.reservation_key,
                agent_run_id=request.agent_run_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                provider_id=request.provider_id,
                model_id=request.model_id,
                state=UsageState.RESERVED,
                reserved_input_tokens=request.reserved_input_tokens,
                reserved_output_tokens=request.reserved_output_tokens,
                reserved_cost_nano_usd=request.reserved_cost_nano_usd,
                reserved_at=request.now,
            )
            self._rows[key] = event
            return UsageReservationResult(event, True, breach)

    def _breach(
        self, request: UsageReservationRequest, admission: UsageAdmission
    ) -> str | None:
        day, month = _day(admission.day_start), _day(admission.month_start)
        day_tokens = month_tokens = day_cost = 0
        for (org, d, *_), (_, tin, tout, cost, _) in self._daily.items():
            if org != request.org_id:
                continue
            if d == day:
                day_tokens += tin + tout
                day_cost += cost
            if d >= month:
                month_tokens += tin + tout
        for row in self._rows.values():
            if (
                row.org_id != request.org_id
                or row.state not in (UsageState.RESERVED, UsageState.UNKNOWN)
                or row.reserved_at < admission.month_start
            ):
                continue
            tokens = row.reserved_input_tokens + row.reserved_output_tokens
            month_tokens += tokens
            if row.reserved_at >= admission.day_start:
                day_tokens += tokens
                day_cost += row.reserved_cost_nano_usd or 0
        return breached_cap(
            request, admission, Committed(day_tokens, month_tokens, day_cost)
        )

    def settle(self, settlement: UsageSettlement) -> bool:
        """Upsert usage; only the first settle updates the rollup."""
        key = (settlement.org_id, settlement.reservation_key)
        with self._lock:
            current = self._rows.get(key)
            if current is not None and current.state is UsageState.SETTLED:
                return False
            base = current or UsageEvent(
                org_id=settlement.org_id,
                workspace_id=settlement.workspace_id,
                reservation_key=settlement.reservation_key,
                agent_run_id=settlement.agent_run_id,
                run_id=settlement.run_id,
                agent_id=settlement.agent_id,
                state=UsageState.RESERVED,
                reserved_input_tokens=0,
                reserved_output_tokens=0,
                reserved_at=settlement.now,
            )
            self._rows[key] = base.model_copy(
                update={
                    "state": UsageState.SETTLED,
                    "provider_id": settlement.provider_id or base.provider_id,
                    "model_id": settlement.model_id or base.model_id,
                    "input_tokens": settlement.input_tokens,
                    "output_tokens": settlement.output_tokens,
                    "estimated_cost_nano_usd": settlement.estimated_cost_nano_usd,
                    "outcome": settlement.outcome,
                    "settled_at": settlement.now,
                }
            )
            cost = settlement.estimated_cost_nano_usd
            bucket = self._daily[
                (
                    settlement.org_id,
                    _day(settlement.now),
                    settlement.agent_id,
                    settlement.provider_id or "unknown",
                    settlement.model_id or "unknown",
                )
            ]
            bucket[0] += 1
            bucket[1] += settlement.input_tokens
            bucket[2] += settlement.output_tokens
            bucket[3] += cost or 0
            bucket[4] += 1 if cost is None else 0
            return True

    def _resolve(
        self, state: UsageState, org_id: str, key: str, outcome: str, now: datetime
    ) -> bool:
        with self._lock:
            row = self._rows.get((org_id, key))
            if row is None or row.state is not UsageState.RESERVED:
                return False
            self._rows[(org_id, key)] = row.model_copy(
                update={"state": state, "outcome": outcome, "settled_at": now}
            )
            return True

    def release(
        self, org_id: str, reservation_key: str, *, outcome: str, now: datetime
    ) -> bool:
        """Reserved to released."""
        return self._resolve(UsageState.RELEASED, org_id, reservation_key, outcome, now)

    def mark_unknown(
        self, org_id: str, reservation_key: str, *, outcome: str, now: datetime
    ) -> bool:
        """Reserved to unknown."""
        return self._resolve(UsageState.UNKNOWN, org_id, reservation_key, outcome, now)

    def get(self, org_id: str, reservation_key: str) -> UsageEvent | None:
        """One row, or None."""
        with self._lock:
            return self._rows.get((org_id, reservation_key))

    def list_reserved_before(
        self, before: datetime, *, limit: int
    ) -> tuple[UsageEvent, ...]:
        """Oldest outstanding reservations first."""
        with self._lock:
            rows = sorted(
                (
                    r
                    for r in self._rows.values()
                    if r.state is UsageState.RESERVED and r.reserved_at < before
                ),
                key=lambda r: r.reserved_at,
            )
        return tuple(rows[:limit])

    def count_state(self, org_id: str, state: UsageState) -> int:
        """Rows of an organization in one state."""
        with self._lock:
            return sum(
                1
                for r in self._rows.values()
                if r.org_id == org_id and r.state is state
            )

    def summary(
        self, org_id: str, start: date, end: date, group_by: UsageGroupBy
    ) -> tuple[UsageTotals, tuple[UsageGroup, ...], int, int]:
        """Rollup totals for the inclusive range plus unknown/in-flight counts."""
        buckets: dict[str, list[int]] = defaultdict(lambda: [0] * 5)
        with self._lock:
            for (org, day, agent, _, model), values in self._daily.items():
                if org != org_id or not start <= day <= end:
                    continue
                key = {
                    UsageGroupBy.AGENT: agent,
                    UsageGroupBy.MODEL: model,
                    UsageGroupBy.DAY: day.isoformat(),
                }[group_by]
                for index, value in enumerate(values):
                    buckets[key][index] += value
            low = datetime.combine(start, datetime.min.time(), UTC)
            high = datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC)
            window = [
                r
                for r in self._rows.values()
                if r.org_id == org_id and low <= r.reserved_at < high
            ]
        groups = tuple(
            UsageGroup(
                key=key,
                calls=v[0],
                input_tokens=v[1],
                output_tokens=v[2],
                total_tokens=v[1] + v[2],
                estimated_cost_nano_usd=v[3],
                unpriced_calls=v[4],
            )
            for key, v in sorted(buckets.items())
        )
        unknown = sum(1 for r in window if r.state is UsageState.UNKNOWN)
        flying = sum(1 for r in window if r.state is UsageState.RESERVED)
        return sum_totals(groups), groups, unknown, flying

    def daily_rows(self, org_id: str) -> list[dict[str, object]]:
        """Rollup rows of one organization."""
        with self._lock:
            items = sorted((k, v) for k, v in self._daily.items() if k[0] == org_id)
        names = ("calls", "input_tokens", "output_tokens")
        return [
            {
                "day": k[1],
                "agent_id": k[2],
                "provider_id": k[3],
                "model_id": k[4],
                **dict(zip(names, v[:3], strict=True)),
                "estimated_cost_nano_usd": v[3],
                "unpriced_calls": v[4],
            }
            for k, v in items
        ]

    def purge_organization(self, limit: int) -> int:
        """Delete up to ``limit`` rows owned by the current organization."""
        org_id = effective_org_id()
        with self._lock:
            keys = [k for k in self._rows if k[0] == org_id][:limit]
            for key in keys:
                del self._rows[key]
            room = max(limit - len(keys), 0)
            daily = [k for k in self._daily if k[0] == org_id][:room]
            for rollup in daily:
                del self._daily[rollup]
            return len(keys) + len(daily)

    def remaining_rows(self) -> int:
        """Rows the current organization still owns."""
        org_id = effective_org_id()
        with self._lock:
            return sum(1 for k in self._rows if k[0] == org_id) + sum(
                1 for k in self._daily if k[0] == org_id
            )

    def purge_before(self, day: date, limit: int) -> int:
        """Retention: drop finished rows and rollups older than ``day``."""
        cutoff = datetime.combine(day, datetime.min.time(), UTC)
        with self._lock:
            old = [
                k
                for k, r in self._rows.items()
                if r.state is not UsageState.RESERVED and r.reserved_at < cutoff
            ][:limit]
            for key in old:
                del self._rows[key]
            stale = [k for k in self._daily if k[1] < day][:limit]
            for rollup in stale:
                del self._daily[rollup]
            return len(old) + len(stale)

    def purge_deleted_organizations(self, limit: int) -> int:
        """Nothing outlives a process here, so there is nothing to sweep."""
        return 0

    def operator_report(
        self, start: date, end: date, *, by: str, org_id: str | None
    ) -> list[OrgUsageRow]:
        """Per-organization usage per day or month."""
        width = {"day": 10, "month": 7}[by]
        buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * 5)
        unknown: dict[tuple[str, str], int] = defaultdict(int)
        with self._lock:
            for (org, day, *_), values in self._daily.items():
                if (org_id and org != org_id) or not start <= day <= end:
                    continue
                for index, value in enumerate(values):
                    buckets[(org, day.isoformat()[:width])][index] += value
            for row in self._rows.values():
                day = _day(row.reserved_at)
                if (
                    row.state is UsageState.UNKNOWN
                    and start <= day <= end
                    and (org_id is None or row.org_id == org_id)
                ):
                    unknown[(row.org_id, day.isoformat()[:width])] += 1
        return [
            OrgUsageRow(
                org_id=org,
                org_name=None,
                period=period,
                totals=UsageTotals(
                    calls=v[0],
                    input_tokens=v[1],
                    output_tokens=v[2],
                    total_tokens=v[1] + v[2],
                    estimated_cost_nano_usd=v[3],
                    unpriced_calls=v[4],
                ),
                unknown_calls=unknown.get((org, period), 0),
            )
            for (org, period), v in sorted(buckets.items())
        ]
