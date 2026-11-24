"""PostgreSQL usage ledger (migration 27): idempotent, row-level secured."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

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
from shared.request_context import effective_org_id

_COLUMNS = (
    "org_id, workspace_id, reservation_key, agent_run_id, run_id, agent_id, "
    "provider_id, model_id, state, reserved_input_tokens, reserved_output_tokens, "
    "reserved_cost_nano_usd, input_tokens, output_tokens, "
    "estimated_cost_nano_usd, outcome, reserved_at, settled_at"
)
_FIELDS = tuple(name.strip() for name in _COLUMNS.split(","))
_ROLLUP_UPSERT = """INSERT INTO usage_daily
        (org_id, day, agent_id, provider_id, model_id, calls, input_tokens,
         output_tokens, estimated_cost_nano_usd, unpriced_calls)
    VALUES (%s, %s, %s, coalesce(%s, 'unknown'), coalesce(%s, 'unknown'),
            1, %s, %s, %s, %s)
    ON CONFLICT (org_id, day, agent_id, provider_id, model_id) DO UPDATE SET
        calls = usage_daily.calls + 1,
        input_tokens = usage_daily.input_tokens + EXCLUDED.input_tokens,
        output_tokens = usage_daily.output_tokens + EXCLUDED.output_tokens,
        estimated_cost_nano_usd = usage_daily.estimated_cost_nano_usd
            + EXCLUDED.estimated_cost_nano_usd,
        unpriced_calls = usage_daily.unpriced_calls + EXCLUDED.unpriced_calls"""
_SETTLE = f"""INSERT INTO usage_ledger ({_COLUMNS}, updated_at)
    VALUES (%(org)s, %(ws)s, %(key)s, %(arun)s, %(run)s, %(agent)s, %(prov)s,
            %(model)s, 'settled', 0, 0, NULL, %(in)s, %(out)s, %(cost)s,
            %(outcome)s, %(now)s, %(now)s, %(now)s)
    ON CONFLICT (org_id, reservation_key) DO UPDATE SET
        state = 'settled', input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        estimated_cost_nano_usd = EXCLUDED.estimated_cost_nano_usd,
        provider_id = coalesce(EXCLUDED.provider_id, usage_ledger.provider_id),
        model_id = coalesce(EXCLUDED.model_id, usage_ledger.model_id),
        outcome = EXCLUDED.outcome, settled_at = EXCLUDED.settled_at,
        updated_at = EXCLUDED.updated_at
    WHERE usage_ledger.state <> 'settled'
    RETURNING 1"""
_RESOLVE = """UPDATE usage_ledger SET state = %s, outcome = %s, settled_at = %s,
        updated_at = %s
    WHERE org_id = %s AND reservation_key = %s AND state = 'reserved'
    RETURNING 1"""
_INSERT = f"""INSERT INTO usage_ledger ({_COLUMNS}, updated_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'reserved', %s, %s, %s,
            NULL, NULL, NULL, NULL, %s, NULL, %s)
    ON CONFLICT (org_id, reservation_key) DO NOTHING RETURNING 1"""
_OUTSTANDING = """SELECT
        coalesce(sum(reserved_input_tokens + reserved_output_tokens)
                 FILTER (WHERE reserved_at >= %s), 0),
        coalesce(sum(reserved_cost_nano_usd) FILTER (WHERE reserved_at >= %s), 0),
        coalesce(sum(reserved_input_tokens + reserved_output_tokens), 0)
    FROM usage_ledger
    WHERE org_id = %s AND state IN ('reserved', 'unknown') AND reserved_at >= %s"""
_GROUP_KEYS = {
    UsageGroupBy.AGENT: "agent_id",
    UsageGroupBy.MODEL: "model_id",
    UsageGroupBy.DAY: "to_char(day, 'YYYY-MM-DD')",
}


def _event(row: Any) -> UsageEvent:
    data = dict(zip(_FIELDS, row, strict=True))
    data["state"] = UsageState(data["state"])
    return UsageEvent(**data)


def _day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC)


def _totals(values: Any) -> UsageTotals:
    calls, tokens_in, tokens_out, cost, unpriced = (int(v) for v in values)
    return UsageTotals(
        calls=calls,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        total_tokens=tokens_in + tokens_out,
        estimated_cost_nano_usd=cost,
        unpriced_calls=unpriced,
    )


def _group(key: str, values: Any) -> UsageGroup:
    return UsageGroup(key=key, **_totals(values).model_dump(exclude={"schema_version"}))


def sum_totals(groups: tuple[UsageGroup, ...]) -> UsageTotals:
    """Add up grouped totals (shared with the in-memory ledger)."""
    return UsageTotals(
        calls=sum(g.calls for g in groups),
        input_tokens=sum(g.input_tokens for g in groups),
        output_tokens=sum(g.output_tokens for g in groups),
        total_tokens=sum(g.total_tokens for g in groups),
        estimated_cost_nano_usd=sum(g.estimated_cost_nano_usd for g in groups),
        unpriced_calls=sum(g.unpriced_calls for g in groups),
    )


class PostgresUsageLedger:
    """Ledger over the product store (tenant scope and transactions included)."""

    def __init__(self, store: Any) -> None:
        """Use the store for scoped connections and advisory locks."""
        self._store = store

    # -- reservations ---------------------------------------------------

    def reserve(
        self, request: UsageReservationRequest, admission: UsageAdmission | None
    ) -> UsageReservationResult:
        """Insert the reservation once; a replay returns the existing row."""
        with self._store.transaction() as connection:
            if admission is not None:
                self._store.acquire_advisory_transaction_lock(f"usage:{request.org_id}")
            existing = self._fetch(connection, request.org_id, request.reservation_key)
            if existing is not None:
                return UsageReservationResult(existing, False)
            breach = None
            if admission is not None:
                breach = self._breach(connection, request, admission)
                if breach is not None and admission.enforce:
                    return UsageReservationResult(None, False, breach)
            inserted = connection.execute(
                _INSERT,
                (
                    request.org_id,
                    request.workspace_id,
                    request.reservation_key,
                    request.agent_run_id,
                    request.run_id,
                    request.agent_id,
                    request.provider_id,
                    request.model_id,
                    request.reserved_input_tokens,
                    request.reserved_output_tokens,
                    request.reserved_cost_nano_usd,
                    request.now,
                    request.now,
                ),
            ).fetchone()
            event = self._fetch(connection, request.org_id, request.reservation_key)
        return UsageReservationResult(event, inserted is not None, breach)

    @staticmethod
    def _fetch(connection: Any, org_id: str, key: str) -> UsageEvent | None:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM usage_ledger "
            "WHERE org_id = %s AND reservation_key = %s",
            (org_id, key),
        ).fetchone()
        return None if row is None else _event(row)

    @staticmethod
    def _breach(
        connection: Any, request: UsageReservationRequest, admission: UsageAdmission
    ) -> str | None:
        """Which cap settled plus outstanding plus this reservation would exceed."""
        org = request.org_id
        day_start = admission.day_start
        settled_day = connection.execute(
            "SELECT coalesce(sum(input_tokens + output_tokens), 0), "
            "coalesce(sum(estimated_cost_nano_usd), 0) FROM usage_daily "
            "WHERE org_id = %s AND day = %s",
            (org, _day(day_start)),
        ).fetchone()
        settled_month = connection.execute(
            "SELECT coalesce(sum(input_tokens + output_tokens), 0) "
            "FROM usage_daily WHERE org_id = %s AND day >= %s",
            (org, _day(admission.month_start)),
        ).fetchone()
        outstanding = connection.execute(
            _OUTSTANDING, (day_start, day_start, org, admission.month_start)
        ).fetchone()
        return breached_cap(
            request,
            admission,
            Committed(
                day_tokens=int(settled_day[0]) + int(outstanding[0]),
                month_tokens=int(settled_month[0]) + int(outstanding[2]),
                day_cost_nano_usd=int(settled_day[1]) + int(outstanding[1]),
            ),
        )

    # -- settlement -----------------------------------------------------

    def settle(self, settlement: UsageSettlement) -> bool:
        """Upsert real usage; only the first settle touches the rollup."""
        cost = settlement.estimated_cost_nano_usd
        with self._store.transaction() as connection:
            changed = connection.execute(
                _SETTLE,
                {
                    "org": settlement.org_id,
                    "ws": settlement.workspace_id,
                    "key": settlement.reservation_key,
                    "arun": settlement.agent_run_id,
                    "run": settlement.run_id,
                    "agent": settlement.agent_id,
                    "prov": settlement.provider_id,
                    "model": settlement.model_id,
                    "in": settlement.input_tokens,
                    "out": settlement.output_tokens,
                    "cost": cost,
                    "outcome": settlement.outcome,
                    "now": settlement.now,
                },
            ).fetchone()
            if changed is None:
                return False
            connection.execute(
                _ROLLUP_UPSERT,
                (
                    settlement.org_id,
                    _day(settlement.now),
                    settlement.agent_id,
                    settlement.provider_id,
                    settlement.model_id,
                    settlement.input_tokens,
                    settlement.output_tokens,
                    cost or 0,
                    1 if cost is None else 0,
                ),
            )
        return True

    def release(
        self, org_id: str, reservation_key: str, *, outcome: str, now: datetime
    ) -> bool:
        """Reserved to released; any other state is left alone."""
        return self._resolve("released", org_id, reservation_key, outcome, now)

    def mark_unknown(
        self, org_id: str, reservation_key: str, *, outcome: str, now: datetime
    ) -> bool:
        """Reserved to unknown; the reservation stays as the charged bound."""
        return self._resolve("unknown", org_id, reservation_key, outcome, now)

    def _resolve(
        self, state: str, org_id: str, key: str, outcome: str, now: datetime
    ) -> bool:
        with self._store.transaction() as connection:
            row = connection.execute(
                _RESOLVE, (state, outcome, now, now, org_id, key)
            ).fetchone()
        return row is not None

    # -- reads ----------------------------------------------------------

    def get(self, org_id: str, reservation_key: str) -> UsageEvent | None:
        """Return one row (invisible to other tenants through row security)."""
        with self._store.connection() as connection:
            return self._fetch(connection, org_id, reservation_key)

    def list_reserved_before(
        self, before: datetime, *, limit: int
    ) -> tuple[UsageEvent, ...]:
        """Oldest outstanding reservations first (system scope ``usage``)."""
        with self._store.connection() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM usage_ledger WHERE state = 'reserved' "
                "AND reserved_at < %s ORDER BY reserved_at LIMIT %s",
                (before, limit),
            ).fetchall()
        return tuple(_event(row) for row in rows)

    def count_state(self, org_id: str, state: UsageState) -> int:
        """Number of rows in one state."""
        with self._store.connection() as connection:
            row = connection.execute(
                "SELECT count(*) FROM usage_ledger WHERE org_id = %s AND state = %s",
                (org_id, state.value),
            ).fetchone()
        return int(row[0])

    def summary(
        self, org_id: str, start: date, end: date, group_by: UsageGroupBy
    ) -> tuple[UsageTotals, tuple[UsageGroup, ...], int, int]:
        """Settled totals from the rollup plus unknown and in-flight counts."""
        with self._store.connection() as connection:
            rows = connection.execute(
                f"""SELECT {_GROUP_KEYS[group_by]} AS key, sum(calls),
                           sum(input_tokens), sum(output_tokens),
                           sum(estimated_cost_nano_usd), sum(unpriced_calls)
                    FROM usage_daily WHERE org_id = %s AND day BETWEEN %s AND %s
                    GROUP BY 1 ORDER BY 1""",
                (org_id, start, end),
            ).fetchall()
            counts = connection.execute(
                """SELECT count(*) FILTER (WHERE state = 'unknown'),
                          count(*) FILTER (WHERE state = 'reserved')
                   FROM usage_ledger WHERE org_id = %s
                     AND reserved_at >= %s AND reserved_at < %s""",
                (org_id, _midnight(start), _midnight(end + timedelta(days=1))),
            ).fetchone()
        groups = tuple(_group(str(r[0]), r[1:]) for r in rows)
        return sum_totals(groups), groups, int(counts[0]), int(counts[1])

    def daily_rows(self, org_id: str) -> list[dict[str, object]]:
        """All rollup rows of one organization, oldest first."""
        with self._store.connection() as connection:
            rows = connection.execute(
                """SELECT day, agent_id, provider_id, model_id, calls, input_tokens,
                          output_tokens, estimated_cost_nano_usd, unpriced_calls
                   FROM usage_daily WHERE org_id = %s
                   ORDER BY day, agent_id, provider_id, model_id""",
                (org_id,),
            ).fetchall()
        names = [
            "day",
            "agent_id",
            "provider_id",
            "model_id",
            "calls",
            "input_tokens",
            "output_tokens",
            "estimated_cost_nano_usd",
            "unpriced_calls",
        ]
        return [dict(zip(names, row, strict=True)) for row in rows]

    # -- deletion and retention ----------------------------------------

    def purge_organization(self, limit: int) -> int:
        """Delete up to ``limit`` ledger and rollup rows of the current org."""
        org_id = effective_org_id()
        removed = 0
        with self._store.transaction() as connection:
            for table in ("usage_ledger", "usage_daily"):
                removed += connection.execute(
                    f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM {table} "
                    "WHERE org_id = %s LIMIT %s)",
                    (org_id, max(limit - removed, 0)),
                ).rowcount
        return removed

    def remaining_rows(self) -> int:
        """Rows the current organization still owns in both tables."""
        org_id = effective_org_id()
        with self._store.connection() as connection:
            row = connection.execute(
                "SELECT (SELECT count(*) FROM usage_ledger WHERE org_id = %s) + "
                "(SELECT count(*) FROM usage_daily WHERE org_id = %s)",
                (org_id, org_id),
            ).fetchone()
        return int(row[0])

    def purge_before(self, day: date, limit: int) -> int:
        """Retention for live organizations: drop finished rows older than ``day``."""
        with self._store.transaction() as connection:
            ledger = connection.execute(
                "DELETE FROM usage_ledger WHERE ctid IN (SELECT ctid FROM "
                "usage_ledger WHERE state <> 'reserved' AND reserved_at < %s "
                "LIMIT %s)",
                (_midnight(day), limit),
            ).rowcount
            rollup = connection.execute(
                "DELETE FROM usage_daily WHERE ctid IN (SELECT ctid FROM "
                "usage_daily WHERE day < %s LIMIT %s)",
                (day, limit),
            ).rowcount
        return int(ledger + rollup)

    def purge_deleted_organizations(self, limit: int) -> int:
        """A call in flight at deletion may settle after the purge; remove that."""
        removed = 0
        with self._store.transaction() as connection:
            for table in ("usage_ledger", "usage_daily"):
                removed += connection.execute(
                    f"DELETE FROM {table} WHERE ctid IN (SELECT t.ctid FROM {table} t "
                    "WHERE EXISTS (SELECT 1 FROM product_records p WHERE "
                    "p.collection = 'organization_lifecycle' AND "
                    "p.record_id = t.org_id AND p.payload->>'state' = 'deleted') "
                    "LIMIT %s)",
                    (max(limit - removed, 0),),
                ).rowcount
        return removed

    # -- operator report ------------------------------------------------

    def operator_report(
        self, start: date, end: date, *, by: str, org_id: str | None
    ) -> list[OrgUsageRow]:
        """Per-organization tokens and cost per day or month (cross-tenant)."""
        pattern = {"day": "YYYY-MM-DD", "month": "YYYY-MM"}[by]
        scope = "AND d.org_id = %s" if org_id else ""
        params: list[Any] = [pattern, start, end] + ([org_id] if org_id else [])
        with self._store.connection() as connection:
            rows = connection.execute(
                f"""SELECT d.org_id, o.payload->>'name', to_char(d.day, %s),
                           sum(d.calls), sum(d.input_tokens), sum(d.output_tokens),
                           sum(d.estimated_cost_nano_usd), sum(d.unpriced_calls)
                    FROM usage_daily d LEFT JOIN product_records o
                      ON o.collection = 'organizations' AND o.record_id = d.org_id
                    WHERE d.day BETWEEN %s AND %s {scope}
                    GROUP BY 1, 2, 3 ORDER BY 1, 3""",
                params,
            ).fetchall()
            unknown = connection.execute(
                f"""SELECT d.org_id, to_char(d.reserved_at AT TIME ZONE 'UTC', %s),
                           count(*)
                    FROM usage_ledger d WHERE d.state = 'unknown'
                      AND (d.reserved_at AT TIME ZONE 'UTC')::date BETWEEN %s AND %s
                      {scope} GROUP BY 1, 2""",
                params,
            ).fetchall()
        unknown_by = {(r[0], r[1]): int(r[2]) for r in unknown}
        return [
            OrgUsageRow(
                org_id=r[0],
                org_name=r[1],
                period=r[2],
                totals=_totals(r[3:]),
                unknown_calls=unknown_by.get((r[0], r[2]), 0),
            )
            for r in rows
        ]
