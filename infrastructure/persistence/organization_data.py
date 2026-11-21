"""Bounded organization-wide export paging and purge for both storage modes.

PostgreSQL works on the single ``product_records`` table through the process
store, so every statement runs under the caller's row-level-security scope in
addition to its explicit ``org_id`` filter. The in-memory adapter mirrors the
behavior over its repositories for local development and contract tests.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from application.ports.organization_data import RetainRule
from application.ports.product_repositories import ProductRepositorySet
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    TaskTurn,
    TaskTurnStatus,
)
from infrastructure.persistence.memory_repository import InMemoryRepository

_FIELD = "abcdefghijklmnopqrstuvwxyz_"

# Quarantined and built-in descriptors need operator resolution; the database
# guard refuses to delete them, so purge leaves them out instead of failing.
_PLUGIN_SAFE = (
    " AND NOT (p.collection = 'plugins' AND EXISTS (SELECT 1 FROM "
    "product_plugin_ownership o WHERE o.plugin_id = p.record_id "
    "AND o.state IN ('builtin', 'quarantined')))"
)


def _iso(value: datetime) -> str:
    """Match the JSON datetime form the contracts serialize."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _retain_clause(retain: RetainRule | None) -> tuple[str, tuple[Any, ...]]:
    """Return SQL excluding rows whose payload field starts with a prefix."""
    if retain is None:
        return "", ()
    field, prefixes = retain
    if not field or any(char not in _FIELD for char in field):
        raise ValueError("retain field must be a simple lowercase name")
    patterns = [prefix.replace("%", r"\%") + "%" for prefix in prefixes]
    return " AND NOT (COALESCE(p.payload->>%s, '') LIKE ANY(%s))", (field, patterns)


class PostgresOrganizationData:
    """Organization-confined page, cancel and delete statements."""

    def __init__(self, store: Any) -> None:
        """Bind the process store whose connections carry the tenant scope."""
        self._store = store

    def _guard(self, connection: Any) -> str:
        """Include the descriptor guard once the ownership tables exist."""
        row = connection.execute(
            "SELECT to_regclass('product_plugin_ownership') IS NOT NULL"
        ).fetchone()
        return _PLUGIN_SAFE if row and row[0] else ""

    def page(
        self, collection: str, org_id: str, *, after: str | None, limit: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return one record-id keyset page of an organization's collection."""
        with self._store.connection() as connection:
            rows = connection.execute(
                """SELECT record_id, payload FROM product_records
                   WHERE collection = %s AND org_id = %s
                     AND (%s::text IS NULL OR record_id > %s::text)
                   ORDER BY record_id LIMIT %s""",
                (collection, org_id, after, after, limit),
            ).fetchall()
        return [(str(row[0]), json.loads(json.dumps(row[1]))) for row in rows]

    def delete_batch(
        self,
        collection: str,
        org_id: str,
        *,
        limit: int,
        retain: RetainRule | None = None,
    ) -> int:
        """Delete up to ``limit`` rows of one collection in record-id order."""
        clause, params = _retain_clause(retain)
        with self._store.connection() as connection:
            guard = self._guard(connection)
            result = connection.execute(
                "DELETE FROM product_records WHERE (collection, record_id) IN ("
                "SELECT p.collection, p.record_id FROM product_records p "
                "WHERE p.collection = %s AND p.org_id = %s"
                + guard
                + clause
                + " ORDER BY p.record_id LIMIT %s)",
                (collection, org_id, *params, limit),
            )
        return int(result.rowcount)

    def delete_remaining_batch(
        self, org_id: str, *, keep_collections: Collection[str], limit: int
    ) -> int:
        """Sweep rows in collections this release does not know by name."""
        with self._store.connection() as connection:
            guard = self._guard(connection)
            result = connection.execute(
                "DELETE FROM product_records WHERE (collection, record_id) IN ("
                "SELECT p.collection, p.record_id FROM product_records p "
                "WHERE p.org_id = %s AND p.collection <> ALL(%s)"
                + guard
                + " ORDER BY p.collection, p.record_id LIMIT %s)",
                (org_id, list(keep_collections), limit),
            )
        return int(result.rowcount)

    def count_remaining(self, org_id: str, *, keep_collections: Collection[str]) -> int:
        """Count rows outside the kept collections, for verification and status."""
        with self._store.connection() as connection:
            row = connection.execute(
                "SELECT count(*) FROM product_records WHERE org_id = %s "
                "AND collection <> ALL(%s)",
                (org_id, list(keep_collections)),
            ).fetchone()
        return int(row[0]) if row else 0

    def cancel_work(self, org_id: str, *, now: datetime, limit: int) -> int:
        """Cancel queued/running jobs and queued turns with contract-valid state."""
        stamp = _iso(now)
        total = 0
        with self._store.connection() as connection:
            for collection, old, new, finish in (
                ("execution_jobs", "queued", "cancelled", True),
                ("execution_jobs", "running", "cancel_requested", False),
                ("task_turns", "queued", "cancelled", True),
            ):
                patch = (
                    "jsonb_build_object('status', %s::text, 'updated_at', %s::text"
                    + (", 'finished_at', %s::text)" if finish else ")")
                )
                params: tuple[Any, ...] = (new, stamp) + ((stamp,) if finish else ())
                result = connection.execute(
                    "UPDATE product_records SET payload = payload || " + patch + ", "
                    "updated_at = now() WHERE (collection, record_id) IN ("
                    "SELECT collection, record_id FROM product_records "
                    "WHERE collection = %s AND org_id = %s "
                    "AND payload->>'status' = %s ORDER BY record_id LIMIT %s)",
                    (*params, collection, org_id, old, limit),
                )
                total += int(result.rowcount)
        return total


class MemoryOrganizationData:
    """The same operations over in-memory repositories."""

    def __init__(self, repositories: ProductRepositorySet) -> None:
        """Bind the repository set that owns every collection."""
        self._repositories = repositories

    def _repository(self, collection: str) -> InMemoryRepository[Any] | None:
        try:
            repo: InMemoryRepository[Any] = self._repositories.get(collection)  # type: ignore[assignment]
        except KeyError:
            return None
        return repo

    def _owned(
        self, repo: InMemoryRepository[Any], collection: str, org_id: str
    ) -> list[tuple[str, Any]]:
        """List (key, record) pairs of one collection owned by the organization."""
        with repo._lock:
            owners = getattr(repo, "_owner_ids", None)
            pairs = [
                (key, record)
                for key, record in repo._records.items()
                if (
                    owners.get(key) == org_id
                    if collection == "plugins" and owners is not None
                    else getattr(record, "org_id", None) == org_id
                )
            ]
        return sorted(pairs, key=lambda pair: pair[0])

    def page(
        self, collection: str, org_id: str, *, after: str | None, limit: int
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return a keyset page of one collection's JSON payloads."""
        repo = self._repository(collection)
        if repo is None:
            return []
        pairs = [
            (key, record.model_dump(mode="json"))
            for key, record in self._owned(repo, collection, org_id)
            if after is None or key > after
        ]
        return pairs[:limit]

    def delete_batch(
        self,
        collection: str,
        org_id: str,
        *,
        limit: int,
        retain: RetainRule | None = None,
    ) -> int:
        """Delete up to ``limit`` owned records, honoring the retain rule."""
        repo = self._repository(collection)
        if repo is None:
            return 0
        removed = 0
        for key, record in self._owned(repo, collection, org_id):
            if removed >= limit:
                break
            if retain is not None and str(getattr(record, retain[0], "")).startswith(
                retain[1]
            ):
                continue
            repo.delete(key)
            removed += 1
        return removed

    def delete_remaining_batch(
        self, org_id: str, *, keep_collections: Collection[str], limit: int
    ) -> int:
        """Nothing is unnamed in memory; every collection is a known repository."""
        del org_id, keep_collections, limit
        return 0

    def count_remaining(self, org_id: str, *, keep_collections: Collection[str]) -> int:
        """Count owned records outside the kept collections."""
        total = 0
        for name in self._repositories.names():
            repo = self._repository(name)
            if repo is not None and name not in keep_collections:
                total += len(self._owned(repo, name, org_id))
        return total

    def cancel_work(self, org_id: str, *, now: datetime, limit: int) -> int:
        """Cancel queued/running jobs and queued task turns."""
        changed = 0
        jobs = self._repository("execution_jobs")
        turns = self._repository("task_turns")
        for repo, status, target in (
            (jobs, ExecutionJobStatus.QUEUED, ExecutionJobStatus.CANCELLED),
            (jobs, ExecutionJobStatus.RUNNING, ExecutionJobStatus.CANCEL_REQUESTED),
            (turns, TaskTurnStatus.QUEUED, TaskTurnStatus.CANCELLED),
        ):
            if repo is None:
                continue
            for key, record in self._owned(repo, "", org_id)[:limit]:
                if getattr(record, "status", None) is not status:
                    continue
                finished = None if target.value == "cancel_requested" else now
                update: dict[str, Any] = {"status": target, "updated_at": now}
                if finished is not None:
                    update["finished_at"] = finished
                cls = ExecutionJob if isinstance(record, ExecutionJob) else TaskTurn
                repo._records[key] = cls.model_validate(
                    record.model_copy(update=update).model_dump()
                )
                changed += 1
        return changed
