"""Row counts behind atomic admission, for PostgreSQL and in-memory storage.

Every count reads the source of truth and runs on the caller's active
transaction connection, after the caller took the resource's advisory lock.
The SQL is assembled only from the constants below (never from request data),
so each query keeps literal collection and status predicates that the partial
indexes of migration 25 can serve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from application.ports.admission import (
    AGENTS,
    QUEUED_WORK,
    SCHEDULES,
    SEATS,
    TASKS,
    TOOL_CALLS_PER_DAY,
    WORKFLOWS,
)
from application.ports.product_repositories import ProductRepositorySet
from application.ports.repository import Repository
from contracts.v1.base import ProductContract
from shared.clock import utc_now


@dataclass(frozen=True)
class CountSpec:
    """The rows that consume one resource of an organization."""

    collections: tuple[str, ...]
    status_in: tuple[str, ...] = ()
    status_not_in: tuple[str, ...] = ()
    window: timedelta | None = None


SPECS: dict[str, CountSpec] = {
    QUEUED_WORK: CountSpec(("task_turns", "execution_jobs"), status_in=("queued",)),
    TASKS: CountSpec(("tasks",)),
    SEATS: CountSpec(("users",), status_not_in=("deleted",)),
    SCHEDULES: CountSpec(("workflow_schedules",), status_in=("active",)),
    AGENTS: CountSpec(("agents",), status_not_in=("archived",)),
    WORKFLOWS: CountSpec(("workflows",), status_not_in=("archived",)),
    TOOL_CALLS_PER_DAY: CountSpec(("tool_invocations",), window=timedelta(days=1)),
}


def created_at(record: object) -> datetime:
    """Read the creation time of a stored contract."""
    value: datetime = record.created_at  # type: ignore[attr-defined]
    return value


def _literal(values: tuple[str, ...]) -> str:
    """Quote constant words for SQL; the inputs are module constants only."""
    for value in values:
        if not value.replace("_", "").isalnum():
            raise ValueError("unsafe literal")
    return ", ".join(f"'{value}'" for value in values)


def _statement(collection: str, spec: CountSpec) -> str:
    """Build one literal-predicate count query for ``collection``."""
    sql = (
        "SELECT count(*) FROM product_records "
        f"WHERE collection = {_literal((collection,))} AND org_id = %s"
    )
    if spec.status_in:
        sql += f" AND payload->>'status' IN ({_literal(spec.status_in)})"
    if spec.status_not_in:
        sql += f" AND payload->>'status' NOT IN ({_literal(spec.status_not_in)})"
    if spec.window is not None:
        sql += " AND created_at >= %s"
    return sql


class PostgresAdmissionCounts:
    """Count rows with SQL on the store's active (or a fresh) connection."""

    def __init__(self, store: Any) -> None:
        """Bind the ``ProductPostgresStore`` that owns the connection scope."""
        self._store = store

    def count(self, org_id: str, resource: str) -> int:
        """Sum the organization's rows across the resource's collections."""
        spec = SPECS[resource]
        total = 0
        with self._store.connection() as connection:
            for collection in spec.collections:
                params: tuple[Any, ...] = (org_id,)
                if spec.window is not None:
                    params += (utc_now() - spec.window,)
                row = connection.execute(_statement(collection, spec), params)
                total += int(row.fetchone()[0])
        return total

    def global_queued(self) -> int:
        """Count queued work everywhere through a definer function (counts only)."""
        with self._store.connection() as connection:
            row = connection.execute("SELECT weaves_global_queued_work()").fetchone()
        return int(row[0])


class MemoryAdmissionCounts:
    """Count in-memory repository records with the same rules as the SQL."""

    def __init__(self, repositories: ProductRepositorySet) -> None:
        """Bind the process repositories."""
        self._repositories = repositories

    def count(self, org_id: str, resource: str) -> int:
        """Count matching records of the organization."""
        spec = SPECS[resource]
        cutoff = utc_now() - spec.window if spec.window is not None else None
        total = 0
        for collection in spec.collections:
            repository: Repository[ProductContract] = self._repositories.get(collection)
            for record in repository.list_scoped(org_id):
                status = str(getattr(record, "status", ""))
                if spec.status_in and status not in spec.status_in:
                    continue
                if status in spec.status_not_in:
                    continue
                if cutoff is not None and created_at(record) < cutoff:
                    continue
                total += 1
        return total

    def global_queued(self) -> int:
        """Count queued task turns and execution jobs of every organization."""
        total = 0
        for collection in ("task_turns", "execution_jobs"):
            repository: Repository[ProductContract] = self._repositories.get(collection)
            total += sum(
                1
                for record in repository.list()
                if str(getattr(record, "status", "")) == "queued"
            )
        return total
