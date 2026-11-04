"""PostgreSQL-backed JSONB product repository implementation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Generic

from application.ports.repository import TContract, _updated_at
from contracts.v1.base import OpaqueId
from infrastructure.persistence.postgres_query_repository import (
    PostgresQueryRepositoryMixin,
)
from shared.errors import IdempotencyKeyAlreadyExists


class PostgresRepository(PostgresQueryRepositoryMixin[TContract], Generic[TContract]):
    """JSONB-backed repository with tenant scope stored as queryable columns."""

    def __init__(
        self,
        pool: Any,
        collection: str,
        id_field: str,
        record_type: type[TContract],
    ) -> None:
        """Initialize the adapter with its identifier or resource configuration."""
        self._pool = pool
        self._collection = collection
        self._id_field = id_field
        self._record_type = record_type

    def create(self, record: TContract) -> TContract:
        """Insert a new record and reject an existing identifier."""
        from psycopg import errors
        from psycopg.types.json import Jsonb

        record_id = self._record_id(record)
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """INSERT INTO product_records
                       (collection, record_id, org_id, workspace_id,
                        idempotency_key, requested_by_principal_id, payload, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))""",
                    (
                        self._collection,
                        record_id,
                        self._scope(record, "org_id"),
                        self._scope(record, "workspace_id"),
                        getattr(record, "idempotency_key", None),
                        getattr(record, "requested_by_principal_id", None),
                        Jsonb(record.model_dump(mode="json")),
                        getattr(record, "created_at", None),
                    ),
                )
        except errors.UniqueViolation as exc:
            if getattr(exc.diag, "constraint_name", None) in {
                "product_records_run_idempotency_uq",
                "product_records_execution_job_idempotency_uq",
                "product_records_task_idempotency_uq",
                "product_records_task_turn_idempotency_uq",
            }:
                raise IdempotencyKeyAlreadyExists(
                    "workflow run idempotency key already exists"
                ) from exc
            raise ValueError(f"{self._id_field} already exists: {record_id}") from exc
        return self._clone(record)

    def put(self, record: TContract) -> TContract:
        """Insert or replace the record with this identifier."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            connection.execute(
                """INSERT INTO product_records
                   (collection, record_id, org_id, workspace_id,
                    idempotency_key, requested_by_principal_id, payload)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (collection, record_id) DO UPDATE SET
                     org_id = EXCLUDED.org_id,
                     workspace_id = EXCLUDED.workspace_id,
                     idempotency_key = EXCLUDED.idempotency_key,
                     requested_by_principal_id = EXCLUDED.requested_by_principal_id,
                     payload = EXCLUDED.payload,
                     updated_at = now()""",
                (
                    self._collection,
                    self._record_id(record),
                    self._scope(record, "org_id"),
                    self._scope(record, "workspace_id"),
                    getattr(record, "idempotency_key", None),
                    getattr(record, "requested_by_principal_id", None),
                    Jsonb(record.model_dump(mode="json")),
                ),
            )
        return self._clone(record)

    def get(self, record_id: OpaqueId) -> TContract:
        """Return one detached record by its identifier."""
        with self._pool.connection() as connection:
            row = connection.execute(
                """SELECT payload FROM product_records
                   WHERE collection = %s AND record_id = %s""",
                (self._collection, str(record_id)),
            ).fetchone()
        if row is None:
            raise KeyError(f"{self._id_field} not found: {record_id}")
        return self._deserialize(row[0])

    def get_scoped(
        self,
        record_id: OpaqueId,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> TContract:
        """Return one record only when it matches the requested tenant scope."""
        query = """SELECT payload FROM product_records
                    WHERE collection = %s AND record_id = %s AND org_id = %s"""
        params: tuple[object, ...] = (self._collection, str(record_id), str(org_id))
        if workspace_id is not None:
            query += " AND workspace_id = %s"
            params += (str(workspace_id),)
        with self._pool.connection() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            raise KeyError(f"{self._id_field} not found in requested scope")
        return self._deserialize(row[0])

    def delete(self, record_id: OpaqueId) -> None:
        """Remove the record with this identifier."""
        with self._pool.connection() as connection:
            result = connection.execute(
                "DELETE FROM product_records WHERE collection = %s AND record_id = %s",
                (self._collection, str(record_id)),
            )
            if result.rowcount == 0:
                raise KeyError(f"{self._id_field} not found: {record_id}")

    def consume(self, record_id: OpaqueId) -> TContract:
        """Atomically return and remove a one-time record."""
        with self._pool.connection() as connection:
            row = connection.execute(
                """DELETE FROM product_records
                   WHERE collection = %s AND record_id = %s
                   RETURNING payload""",
                (self._collection, str(record_id)),
            ).fetchone()
        if row is None:
            raise KeyError(f"{self._id_field} not found: {record_id}")
        return self._deserialize(row[0])

    def list(self) -> tuple[TContract, ...]:
        """Return all stored records as detached values."""
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT payload FROM product_records
                   WHERE collection = %s
                   ORDER BY record_id""",
                (self._collection,),
            ).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_due(self, due_before: datetime, *, limit: int) -> tuple[TContract, ...]:
        """Return due records ordered for bounded worker processing."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if due_before.tzinfo is None or due_before.utcoffset() is None:
            raise ValueError("due_before must include a timezone")
        cutoff = due_before.astimezone(UTC).isoformat().replace("+00:00", "Z")
        with self._pool.connection() as connection:
            rows = connection.execute(
                """SELECT payload FROM product_records
                   WHERE collection = %s AND payload->>'status' = 'active'
                     AND payload->>'next_run_at' <= %s
                   ORDER BY payload->>'next_run_at', record_id
                   LIMIT %s""",
                (self._collection, cutoff, limit),
            ).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_scoped(
        self, org_id: OpaqueId, workspace_id: OpaqueId | None = None
    ) -> tuple[TContract, ...]:
        """Return records visible in the requested tenant scope."""
        query = """SELECT payload FROM product_records
                    WHERE collection = %s AND org_id = %s"""
        params: tuple[object, ...] = (self._collection, str(org_id))
        if workspace_id is not None:
            query += " AND workspace_id = %s"
            params += (str(workspace_id),)
        query += " ORDER BY record_id"
        with self._pool.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def find_by_idempotency_key(
        self,
        key: str,
        requested_by_principal_id: str,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
    ) -> TContract | None:
        """Find a request record by its principal-scoped idempotency key."""
        with self._pool.connection() as connection:
            row = connection.execute(
                """SELECT payload FROM product_records
                   WHERE collection = %s AND org_id = %s AND workspace_id = %s
                     AND requested_by_principal_id = %s AND idempotency_key = %s""",
                (
                    self._collection,
                    str(org_id),
                    str(workspace_id),
                    requested_by_principal_id,
                    key,
                ),
            ).fetchone()
        return self._deserialize(row[0]) if row is not None else None

    def put_if_status_and_stale(
        self,
        record: TContract,
        *,
        expected_status: str,
        updated_before: datetime,
    ) -> bool:
        """Replace a stale record only when its current status still matches."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            result = connection.execute(
                """UPDATE product_records SET payload = %s, updated_at = %s
                   WHERE collection = %s AND record_id = %s
                     AND payload->>'status' = %s AND updated_at <= %s""",
                (
                    Jsonb(record.model_dump(mode="json")),
                    _updated_at(record),
                    self._collection,
                    self._record_id(record),
                    expected_status,
                    updated_before,
                ),
            )
        return int(result.rowcount) == 1

    def put_if_execution_job_owned(
        self,
        record: TContract,
        *,
        worker_id: str,
        attempts: int,
        expected_status: str = "running",
    ) -> bool:
        """Fence job heartbeat and completion updates to the active claim."""
        return self.put_if_worker_owned(
            record,
            worker_id=worker_id,
            attempts=attempts,
            expected_status=expected_status,
        )

    def put_if_worker_owned(
        self,
        record: TContract,
        *,
        worker_id: str,
        attempts: int,
        expected_status: str = "running",
    ) -> bool:
        """Fence a record update to the current worker lease and attempt."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            result = connection.execute(
                """UPDATE product_records SET payload = %s, updated_at = %s
                   WHERE collection = %s AND record_id = %s
                     AND payload->>'status' = %s
                     AND payload->>'worker_id' = %s
                     AND (payload->>'attempts')::integer = %s""",
                (
                    Jsonb(record.model_dump(mode="json")),
                    _updated_at(record),
                    self._collection,
                    self._record_id(record),
                    expected_status,
                    worker_id,
                    attempts,
                ),
            )
        return int(result.rowcount) == 1

    def put_if_task_state(
        self,
        record: TContract,
        *,
        expected_status: str,
        expected_active_turn_id: str | None,
    ) -> bool:
        """Atomically replace task state only if its turn/status are unchanged."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            result = connection.execute(
                """UPDATE product_records SET payload = %s, updated_at = %s
                   WHERE collection = %s AND record_id = %s
                     AND payload->>'status' = %s
                     AND payload->>'active_turn_id' IS NOT DISTINCT FROM %s""",
                (
                    Jsonb(record.model_dump(mode="json")),
                    _updated_at(record),
                    self._collection,
                    self._record_id(record),
                    expected_status,
                    expected_active_turn_id,
                ),
            )
        return int(result.rowcount) == 1

    def put_if_invitation_pending(
        self, record: TContract, *, expected_token_digest: str
    ) -> bool:
        """Fence invitation acceptance and management to its current token."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            result = connection.execute(
                """UPDATE product_records SET payload = %s, updated_at = %s
                   WHERE collection = %s AND record_id = %s
                     AND payload->>'status' = 'pending'
                     AND payload->>'token_digest' = %s""",
                (
                    Jsonb(record.model_dump(mode="json")),
                    _updated_at(record),
                    self._collection,
                    self._record_id(record),
                    expected_token_digest,
                ),
            )
        return int(result.rowcount) == 1

    def _record_id(self, record: TContract) -> str:
        """Read and normalize the contract identifier configured for this repository."""
        try:
            return str(getattr(record, self._id_field))
        except AttributeError as exc:
            raise TypeError(f"record has no {self._id_field} field") from exc

    @staticmethod
    def _scope(record: TContract, field: str) -> str | None:
        """Return a valid scope field as text when present."""
        value = getattr(record, field, None)
        return str(value) if value is not None else None

    @staticmethod
    def _clone(record: TContract) -> TContract:
        """Round-trip the value through its contract serializer for isolation."""
        return type(record).model_validate_json(record.model_dump_json())

    def _deserialize(self, payload: object) -> TContract:
        """Apply strict contract parsing to the JSONB representation."""
        return self._record_type.model_validate_json(json.dumps(payload))
