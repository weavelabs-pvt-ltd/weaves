"""PostgreSQL and in-memory ``FeedbackAdmin`` (system purpose ``feedback``)."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from typing import Any

from application.ports.repository import Repository
from application.ports.slack_feedback import PERIODS, FeedbackCount
from contracts.v1.slack_feedback import SlackFeedback
from shared.request_context import system_scope

_FORMAT = {"day": "YYYY-MM-DD", "month": "YYYY-MM"}
_PUBLIC = (
    "org_id",
    "answer_ref",
    "turn_id",
    "task_id",
    "agent_run_id",
    "request_id",
    "value",
    "source",
    "at",
    "agent_id",
    "model_id",
    "connector_set",
)


def _bounds(start: date, end: date, by: str) -> tuple[datetime, datetime]:
    if by not in PERIODS:
        raise ValueError("--by must be day or month")
    if start > end:
        raise ValueError("--from must not be after --to")
    first = datetime.combine(start, time.min, tzinfo=UTC)
    last = datetime.combine(end, time.max, tzinfo=UTC)
    return first, last


def _period(moment: datetime, by: str) -> str:
    day = moment.astimezone(UTC).date().isoformat()
    return day if by == "day" else day[:7]


def public_row(payload: dict[str, Any]) -> dict[str, Any]:
    """The fields of a stored row a report may show (no asker of any kind)."""
    return {name: payload.get(name) for name in _PUBLIC}


class PostgresFeedbackAdmin:
    """SQL over ``product_records`` under the ``feedback`` system purpose."""

    def __init__(self, store: Any) -> None:
        """Bind the product store (owner or restricted connection)."""
        self._store = store

    def expire(self, before: datetime, limit: int) -> int:
        """Delete the oldest expired rows, at most ``limit``."""
        with system_scope("feedback"), self._store.connection() as connection:
            return int(
                connection.execute(
                    "DELETE FROM product_records WHERE collection = "
                    "'slack_feedback' AND record_id IN (SELECT record_id FROM "
                    "product_records WHERE collection = 'slack_feedback' AND "
                    "(payload->>'at')::timestamptz < %s "
                    "ORDER BY (payload->>'at')::timestamptz LIMIT %s)",
                    (before, limit),
                ).rowcount
            )

    def counts(
        self, start: date, end: date, *, by: str, org_id: str | None = None
    ) -> list[FeedbackCount]:
        """One row per period, organization, agent, model and connector set."""
        first, last = _bounds(start, end, by)
        with system_scope("feedback"), self._store.connection() as connection:
            found = connection.execute(
                "SELECT to_char((payload->>'at')::timestamptz AT TIME ZONE 'UTC', "
                f"'{_FORMAT[by]}') AS period, org_id, "
                "coalesce(payload->>'agent_id', ''), "
                "coalesce(payload->>'model_id', ''), "
                "coalesce(payload->>'connector_set', ''), "
                "count(*) FILTER (WHERE (payload->>'value')::int = 1), "
                "count(*) FILTER (WHERE (payload->>'value')::int = -1) "
                "FROM product_records WHERE collection = 'slack_feedback' "
                "AND (payload->>'at')::timestamptz BETWEEN %s AND %s "
                "AND (%s::text IS NULL OR org_id = %s) "
                "GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5",
                (first, last, org_id, org_id),
            ).fetchall()
        return [FeedbackCount(*row) for row in found]

    def by_request(self, request_id: str) -> list[dict[str, Any]]:
        """The stored rows of one request id (an unindexed scan: operator use)."""
        with system_scope("feedback"), self._store.connection() as connection:
            found = connection.execute(
                "SELECT payload, org_id FROM product_records WHERE collection = "
                "'slack_feedback' AND payload->>'request_id' = %s "
                "ORDER BY (payload->>'at')::timestamptz",
                (request_id,),
            ).fetchall()
        return [public_row({**payload, "org_id": org}) for payload, org in found]


class InMemoryFeedbackAdmin:
    """The same operations over the in-memory repository (development, tests)."""

    def __init__(self, repository: Repository[SlackFeedback]) -> None:
        """Bind the (unscoped) in-memory repository."""
        self._rows = repository

    def expire(self, before: datetime, limit: int) -> int:
        """Delete the oldest expired rows, at most ``limit``."""
        old = sorted(
            (r for r in self._rows.list() if r.at < before), key=lambda r: r.at
        )
        for row in old[:limit]:
            self._rows.delete(row.id)
        return min(len(old), limit)

    def counts(
        self, start: date, end: date, *, by: str, org_id: str | None = None
    ) -> list[FeedbackCount]:
        """One row per period, organization, agent, model and connector set."""
        first, last = _bounds(start, end, by)
        totals: dict[tuple[str, str, str, str, str], list[int]] = defaultdict(
            lambda: [0, 0]
        )
        for row in self._rows.list():
            if not first <= row.at <= last or org_id not in (None, row.org_id):
                continue
            key = (
                _period(row.at, by),
                row.org_id,
                row.agent_id or "",
                row.model_id or "",
                row.connector_set,
            )
            totals[key][0 if row.value == 1 else 1] += 1
        return [FeedbackCount(*key, *totals[key]) for key in sorted(totals)]

    def by_request(self, request_id: str) -> list[dict[str, Any]]:
        """The stored rows of one request id."""
        rows = sorted(
            (r for r in self._rows.list() if r.request_id == request_id),
            key=lambda r: r.at,
        )
        return [public_row(row.model_dump(mode="json")) for row in rows]
