"""Operator-side feedback report: thumbs per day, agent, model and connector set.

Used only by ``infra/gcp/singlevm/slack_feedback_report.py`` on the VM. It reads
``slack_feedback`` across organizations under the named system purpose
``feedback`` (see ``application/ports/slack_feedback.py``) and returns counts and
ids only: no asker of any kind, no message text, no reason. It feeds Analytics
metric #8 (answer feedback) and, through the request-id lookup, metric #10
(a complaint to its organization, task, run and audit trail).
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any

from application.ports.slack_feedback import PERIODS, FeedbackAdmin

__all__ = ["COLUMNS", "LOOKUP_COLUMNS", "PERIODS", "lookup", "summary", "to_csv"]

COLUMNS = (
    "period",
    "org_id",
    "agent_id",
    "model_id",
    "connector_set",
    "up",
    "down",
    "total",
)
LOOKUP_COLUMNS = (
    "org_id",
    "request_id",
    "turn_id",
    "task_id",
    "agent_run_id",
    "answer_ref",
    "value",
    "source",
    "at",
    "agent_id",
    "model_id",
    "connector_set",
)
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def summary(
    admin: FeedbackAdmin,
    start: date,
    end: date,
    *,
    by: str = "month",
    org_id: str | None = None,
) -> list[dict[str, Any]]:
    """One row per period, organization, agent, model and connector set."""
    return [
        {
            "period": row.period,
            "org_id": row.org_id,
            "agent_id": row.agent_id,
            "model_id": row.model_id,
            "connector_set": row.connector_set,
            "up": row.up,
            "down": row.down,
            "total": row.up + row.down,
        }
        for row in admin.counts(start, end, by=by, org_id=org_id)
    ]


def lookup(admin: FeedbackAdmin, request_id: str) -> list[dict[str, Any]]:
    """The ratings of one request id, for joining a complaint to its run."""
    return admin.by_request(request_id)


def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def to_csv(rows: list[dict[str, Any]], columns: tuple[str, ...] = COLUMNS) -> str:
    """The rows as CSV with spreadsheet formulas neutralized."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_safe_cell(row.get(name)) for name in columns])
    return buffer.getvalue()
