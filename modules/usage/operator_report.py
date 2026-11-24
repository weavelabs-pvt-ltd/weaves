"""Operator-side usage report: tokens and estimated cost per organization.

Used only by ``infra/gcp/singlevm/usage_report.py`` on the VM. It reads the
daily rollup across organizations (the named system purpose ``usage``), so it
never touches prompts, outputs or credentials: only counts and amounts.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Any

from application.ports.usage import UsageLedger
from contracts.v1.usage import NANO_USD_PER_USD
from shared.request_context import system_scope

PERIODS = ("day", "month")
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
COLUMNS = (
    "period",
    "org_id",
    "org_name",
    "calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "unpriced_calls",
    "unknown_calls",
)


def nano_to_usd(nano: int) -> str:
    """Exact decimal USD for an integer nano-USD amount."""
    return format(Decimal(nano) / Decimal(NANO_USD_PER_USD), ".9f")


def build_report(
    ledger: UsageLedger,
    start: date,
    end: date,
    *,
    by: str = "month",
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    """One row per organization and period, oldest period first."""
    if by not in PERIODS:
        raise ValueError("--by must be day or month")
    if start > end:
        raise ValueError("--from must not be after --to")
    with system_scope("usage"):
        rows = ledger.operator_report(start, end, by=by, org_id=org_id)
    report = [
        {
            "period": row.period,
            "org_id": row.org_id,
            "org_name": row.org_name,
            "calls": row.totals.calls,
            "input_tokens": row.totals.input_tokens,
            "output_tokens": row.totals.output_tokens,
            "total_tokens": row.totals.total_tokens,
            "estimated_cost_usd": nano_to_usd(row.totals.estimated_cost_nano_usd),
            "unpriced_calls": row.totals.unpriced_calls,
            "unknown_calls": row.unknown_calls,
        }
        for row in rows
    ]
    return sorted(report, key=lambda r: (r["period"], r["org_id"]))


def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def to_csv(rows: list[dict[str, Any]]) -> str:
    """The report as CSV with spreadsheet formulas neutralized."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([_safe_cell(row[name]) for name in COLUMNS])
    return buffer.getvalue()
