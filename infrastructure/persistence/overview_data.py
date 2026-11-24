"""Aggregate Home metrics for both storage modes.

PostgreSQL answers with ``COUNT(*) FILTER`` over indexed scans (migration 28);
the in-memory adapter computes the same numbers over its repositories for local
development and contract tests.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from application.ports.overview_data import DAILY_HISTORY_DAYS
from application.ports.product_repositories import ProductRepositorySet

_VISIBLE = """(COALESCE(k.payload->>'audience', 'workspace') <> 'private'
               OR k.payload->>'audience_owner_principal_id' = %s)"""
_TURN_TASK_JOIN = """JOIN product_records k ON k.collection = 'tasks'
                        AND k.record_id = t.payload->>'task_id'
                        AND k.org_id = t.org_id AND k.workspace_id = t.workspace_id"""


def _empty_daily() -> dict[str, Any]:
    return {"total": 0, "failed": 0, "succeeded": 0, "cost": Decimal(0), "priced": 0}


def _shape(
    tasks: dict[str, int],
    queued: int,
    running: int,
    daily: dict[str, dict[str, Any]],
    setup: tuple[bool, bool],
) -> dict[str, Any]:
    """The adapter-independent response structure."""
    return {
        "tasks": {"total": sum(tasks.values()), "by_status": tasks},
        "turns": {"queued": queued, "running": running},
        "daily": [
            {
                "date": day,
                "total": row["total"],
                "failed": row["failed"],
                "succeeded": row["succeeded"],
                "cost_usd": str(row["cost"]) if row["priced"] else None,
                "priced": row["priced"],
            }
            for day, row in sorted(daily.items())
        ],
        "setup": {"has_agent": setup[0], "has_active_connection": setup[1]},
    }


class PostgresOverviewData:
    """A few indexed statements per snapshot, under the caller's tenant scope."""

    def __init__(self, store: Any) -> None:
        """Bind the process store whose connections carry the tenant scope."""
        self._store = store

    def snapshot(
        self,
        org_id: str,
        workspace_id: str,
        principal_id: str,
        *,
        now: datetime,
        tz_offset_minutes: int,
    ) -> dict[str, Any]:
        """Return the aggregates in a handful of bounded statements."""
        since = now - timedelta(days=DAILY_HISTORY_DAYS + 1)
        scope = (org_id, workspace_id)
        with self._store.connection() as connection:
            tasks = self._task_counts(connection, scope, principal_id)
            active = self._active_turns(connection, scope, principal_id)
            daily = self._daily(
                connection, scope, principal_id, since, tz_offset_minutes
            )
            setup = (
                self._has_active(connection, "agents", scope),
                self._has_active(connection, "plugin_installations", scope),
            )
        return _shape(tasks, active[0], active[1], daily, setup)

    @staticmethod
    def _task_counts(
        connection: Any, scope: tuple[str, str], principal_id: str
    ) -> dict[str, int]:
        rows = connection.execute(
            f"""SELECT k.payload->>'status', count(*) FROM product_records k
                   WHERE k.collection = 'tasks' AND k.org_id = %s
                     AND k.workspace_id = %s AND {_VISIBLE}
                   GROUP BY 1""",
            (*scope, principal_id),
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    @staticmethod
    def _active_turns(
        connection: Any, scope: tuple[str, str], principal_id: str
    ) -> tuple[int, int]:
        row = connection.execute(
            f"""SELECT count(*) FILTER (WHERE t.payload->>'status' = 'queued'),
                       count(*) FILTER (WHERE t.payload->>'status' = 'running')
                   FROM product_records t {_TURN_TASK_JOIN}
                   WHERE t.collection = 'task_turns' AND t.org_id = %s
                     AND t.workspace_id = %s
                     AND t.payload->>'status' IN ('queued', 'running')
                     AND {_VISIBLE}""",
            (*scope, principal_id),
        ).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _daily(
        connection: Any,
        scope: tuple[str, str],
        principal_id: str,
        since: datetime,
        tz_offset_minutes: int,
    ) -> dict[str, dict[str, Any]]:
        rows = connection.execute(
            f"""SELECT ((t.created_at AT TIME ZONE 'UTC')
                          + make_interval(mins => %s))::date AS day,
                       count(*),
                       count(*) FILTER (WHERE t.payload->>'status' = 'failed'),
                       count(*) FILTER (WHERE t.payload->>'status' = 'succeeded'),
                       COALESCE(sum(
                           (a.payload->>'estimated_cost_usd')::numeric), 0),
                       count(a.record_id) FILTER (
                           WHERE a.payload->>'estimated_cost_usd' IS NOT NULL)
                   FROM product_records t {_TURN_TASK_JOIN}
                   LEFT JOIN product_records a ON a.collection = 'agent_runs'
                    AND a.record_id = t.payload->>'agent_run_id'
                    AND a.org_id = t.org_id AND a.workspace_id = t.workspace_id
                   WHERE t.collection = 'task_turns' AND t.org_id = %s
                     AND t.workspace_id = %s AND t.created_at >= %s
                     AND {_VISIBLE}
                   GROUP BY 1""",
            (tz_offset_minutes, *scope, since, principal_id),
        ).fetchall()
        return {
            str(row[0]): {
                "total": int(row[1]),
                "failed": int(row[2]),
                "succeeded": int(row[3]),
                "cost": Decimal(row[4]),
                "priced": int(row[5]),
            }
            for row in rows
        }

    @staticmethod
    def _has_active(connection: Any, collection: str, scope: tuple[str, str]) -> bool:
        row = connection.execute(
            """SELECT EXISTS (SELECT 1 FROM product_records
                   WHERE collection = %s AND org_id = %s AND workspace_id = %s
                     AND payload->>'status' = 'active')""",
            (collection, *scope),
        ).fetchone()
        return bool(row[0])


class MemoryOverviewData:
    """The same aggregates over in-memory repositories."""

    def __init__(self, repositories: ProductRepositorySet) -> None:
        """Bind the repository set that owns every collection."""
        self._repositories = repositories

    def snapshot(
        self,
        org_id: str,
        workspace_id: str,
        principal_id: str,
        *,
        now: datetime,
        tz_offset_minutes: int,
    ) -> dict[str, Any]:
        """Compute the aggregates by walking the workspace's records."""
        get: Callable[[str], Any] = self._repositories.get
        tasks = {
            task.task_id: task
            for task in get("tasks").list_scoped(org_id, workspace_id)
            if task.audience.value != "private"
            or task.audience_owner_principal_id == principal_id
        }
        by_status: dict[str, int] = defaultdict(int)
        for task in tasks.values():
            by_status[task.status.value] += 1
        runs = {
            run.agent_run_id: run
            for run in get("agent_runs").list_scoped(org_id, workspace_id)
        }
        since = now - timedelta(days=DAILY_HISTORY_DAYS + 1)
        queued = running = 0
        daily: dict[str, dict[str, Any]] = defaultdict(_empty_daily)
        for turn in get("task_turns").list_scoped(org_id, workspace_id):
            if turn.task_id not in tasks:
                continue
            queued += turn.status.value == "queued"
            running += turn.status.value == "running"
            if turn.created_at < since:
                continue
            local = turn.created_at + timedelta(minutes=tz_offset_minutes)
            row = daily[local.date().isoformat()]
            row["total"] += 1
            row["failed"] += turn.status.value == "failed"
            row["succeeded"] += turn.status.value == "succeeded"
            run = runs.get(turn.agent_run_id or "")
            if run is not None and run.estimated_cost_usd is not None:
                row["cost"] += run.estimated_cost_usd
                row["priced"] += 1
        return _shape(
            dict(by_status),
            queued,
            running,
            daily,
            (
                any(
                    item.status.value == "active"
                    for item in get("agents").list_scoped(org_id, workspace_id)
                ),
                any(
                    item.status.value == "active"
                    for item in get("plugin_installations").list_scoped(
                        org_id, workspace_id
                    )
                ),
            ),
        )
