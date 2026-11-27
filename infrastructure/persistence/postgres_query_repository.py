"""PostgreSQL query operations for paginated and worker-facing records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from contracts.v1.base import OpaqueId


class PostgresQueryRepositoryMixin[TContract]:
    """Implement filtered pages and atomic queue claims for repositories."""

    _pool: Any
    _collection: str

    def _deserialize(self, payload: object) -> TContract:
        """Deserialize one repository payload into its declared contract."""
        raise NotImplementedError

    def list_audit_page(
        self,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None,
        *,
        action: str | None,
        target_type: str | None,
        target_id: str | None,
        actor_principal_id: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        cursor_created_at: datetime | None,
        cursor_event_id: str | None,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return a filtered audit page after the supplied keyset cursor."""
        if self._collection != "audit_events":
            raise TypeError("audit event paging requires an audit event repository")
        if limit < 1:
            raise ValueError("limit must be positive")
        query = """SELECT payload FROM product_records
                       WHERE collection = %s AND org_id = %s"""
        params: tuple[object, ...] = (self._collection, str(org_id))
        if workspace_id is not None:
            query += " AND workspace_id = %s"
            params += (str(workspace_id),)
        for field, value in (
            ("action", action),
            ("target_type", target_type),
            ("target_id", target_id),
            ("actor_principal_id", actor_principal_id),
        ):
            if value is not None:
                query += f" AND payload->>'{field}' = %s"
                params += (value,)
        if created_after is not None:
            query += " AND created_at >= %s"
            params += (created_after,)
        if created_before is not None:
            query += " AND created_at <= %s"
            params += (created_before,)
        if cursor_created_at is not None:
            if cursor_event_id is None:
                raise ValueError("cursor event ID is required")
            query += " AND (created_at, record_id) < (%s, %s)"
            params += (cursor_created_at, cursor_event_id)
        query += " ORDER BY created_at DESC, record_id DESC LIMIT %s"
        params += (limit,)
        with self._pool.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def find_by_field(self, field: str, value: str) -> TContract | None:
        """Return the first record matching a validated contract field."""
        if not field.isascii() or not field.replace("_", "a").isalnum():
            raise ValueError("field must be a valid contract field name")
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""SELECT payload FROM product_records
                        WHERE collection = %s AND payload ->> '{field}' = %s LIMIT 1""",
                (self._collection, value),
            ).fetchone()
        return self._deserialize(row[0]) if row is not None else None

    @staticmethod
    def _checked_field(field: str) -> str:
        """Reject anything but a plain contract field name before it is inlined."""
        if not field.isascii() or not field.replace("_", "a").isalnum():
            raise ValueError("field must be a valid contract field name")
        return field

    def list_by_field(
        self, field: str, value: str, *, casefold: bool = False
    ) -> tuple[TContract, ...]:
        """Return all records whose payload field equals the value."""
        field = self._checked_field(field)
        if casefold:
            where = f"lower(payload ->> '{field}') = lower(%s)"
        else:
            where = f"payload ->> '{field}' = %s"
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""SELECT payload FROM product_records
                        WHERE collection = %s AND {where}
                        ORDER BY record_id""",
                (self._collection, value),
            ).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_before(
        self,
        field: str,
        before: datetime,
        *,
        limit: int,
        equals: dict[str, str] | None = None,
    ) -> tuple[TContract, ...]:
        """Return a bounded page of records whose timestamp field is past due."""
        field = self._checked_field(field)
        if limit < 1:
            raise ValueError("limit must be positive")
        if before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must include a timezone")
        cutoff = before.astimezone(UTC).isoformat().replace("+00:00", "Z")
        extra = ""
        params: tuple[object, ...] = (self._collection, cutoff)
        for name, value in (equals or {}).items():
            extra += f" AND payload ->> '{self._checked_field(name)}' = %s"
            params += (value,)
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"""SELECT payload FROM product_records
                        WHERE collection = %s AND payload ->> '{field}' < %s{extra}
                        ORDER BY payload ->> '{field}', record_id LIMIT %s""",
                (*params, limit),
            ).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_task_page(
        self,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
        *,
        status: str | None,
        agent_id: str | None,
        created_by_principal_id: str | None,
        visible_to_principal_id: str,
        cursor_created_at: datetime | None,
        cursor_task_id: str | None,
        limit: int,
        origin_kind: str | None = None,
    ) -> tuple[TContract, ...]:
        """Return a bounded newest-first task page using indexed predicates."""
        if self._collection != "tasks":
            raise TypeError("task paging requires a task repository")
        if limit < 1:
            raise ValueError("limit must be positive")
        query = """SELECT payload FROM product_records
                        WHERE collection = %s AND org_id = %s AND workspace_id = %s"""
        params: tuple[object, ...] = (self._collection, str(org_id), str(workspace_id))
        for field, value in (
            ("status", status),
            ("agent_id", agent_id),
            ("created_by_principal_id", created_by_principal_id),
        ):
            if value is not None:
                query += f" AND payload->>'{field}' = %s"
                params += (value,)
        if origin_kind is not None:
            query += " AND payload->'origin'->>'kind' = %s"
            params += (origin_kind,)
        query += " AND (COALESCE(payload->>'audience', 'workspace') <> 'private'"
        query += " OR payload->>'audience_owner_principal_id' = %s)"
        params += (visible_to_principal_id,)
        if cursor_created_at is not None:
            if cursor_task_id is None:
                raise ValueError("cursor task ID is required")
            query += " AND (created_at, record_id) < (%s, %s)"
            params += (cursor_created_at, cursor_task_id)
        query += " ORDER BY created_at DESC, record_id DESC LIMIT %s"
        params += (limit,)
        with self._pool.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_task_records_page(
        self,
        task_id: OpaqueId,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
        *,
        cursor_created_at: datetime | None,
        cursor_record_id: str | None,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return a bounded newest-first page of one task's messages or turns."""
        if self._collection not in {"task_messages", "task_turns"}:
            raise TypeError("task paging requires a task message or turn repository")
        if limit < 1:
            raise ValueError("limit must be positive")
        query = """SELECT payload FROM product_records
                        WHERE collection = %s AND org_id = %s AND workspace_id = %s
                          AND payload->>'task_id' = %s"""
        params: tuple[object, ...] = (
            self._collection,
            str(org_id),
            str(workspace_id),
            str(task_id),
        )
        if cursor_created_at is not None:
            if cursor_record_id is None:
                raise ValueError("cursor record ID is required")
            query += " AND (created_at, record_id) < (%s, %s)"
            params += (cursor_created_at, cursor_record_id)
        query += " ORDER BY created_at DESC, record_id DESC LIMIT %s"
        params += (limit,)
        with self._pool.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_status_updated_before(
        self,
        status: str,
        updated_before: datetime,
        *,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return a bounded stale-record page for worker recovery."""
        if limit < 1:
            raise ValueError("limit must be positive")
        query = """SELECT payload FROM product_records
                       WHERE collection = %s AND payload->>'status' = %s
                         AND updated_at <= %s"""
        params: tuple[object, ...] = (self._collection, status, updated_before)
        query += " ORDER BY updated_at, record_id LIMIT %s"
        with self._pool.connection() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_by_status(
        self, status: str, *, limit: int, after_id: str | None = None
    ) -> tuple[TContract, ...]:
        """Return a bounded, identifier-ordered page of records in one status."""
        if limit < 1:
            raise ValueError("limit must be positive")
        query = """SELECT payload FROM product_records
                       WHERE collection = %s AND payload->>'status' = %s"""
        params: tuple[object, ...] = (self._collection, status)
        if after_id is not None:
            query += " AND record_id > %s"
            params += (after_id,)
        query += " ORDER BY record_id LIMIT %s"
        with self._pool.connection() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_recent_page(
        self,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None,
        *,
        filters: dict[str, str],
        cursor_created_at: datetime | None,
        cursor_id: str | None,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return one newest-first keyset page of a tenant's records.

        ``filters`` are equality predicates on contract fields (for example
        ``status``). Order is ``(created_at, record_id)`` descending.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        query = """SELECT payload FROM product_records
                       WHERE collection = %s AND org_id = %s"""
        params: tuple[object, ...] = (self._collection, str(org_id))
        if workspace_id is not None:
            query += " AND workspace_id = %s"
            params += (str(workspace_id),)
        for field, value in filters.items():
            query += f" AND payload->>'{self._checked_field(field)}' = %s"
            params += (value,)
        if cursor_created_at is not None:
            if cursor_id is None:
                raise ValueError("cursor record ID is required")
            if getattr(self, "_tenant_keyed", False):
                cursor_id = f"{org_id}:{cursor_id}"
            query += " AND (created_at, record_id) < (%s, %s)"
            params += (cursor_created_at, cursor_id)
        query += " ORDER BY created_at DESC, record_id DESC LIMIT %s"
        with self._pool.connection() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def get_many_scoped(
        self,
        record_ids: tuple[str, ...],
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> tuple[TContract, ...]:
        """Return the tenant's records among ``record_ids`` (missing ones skipped)."""
        if not record_ids:
            return ()
        query = """SELECT payload FROM product_records
                       WHERE collection = %s AND record_id = ANY(%s) AND org_id = %s"""
        params: tuple[object, ...] = (self._collection, list(record_ids), str(org_id))
        if workspace_id is not None:
            query += " AND workspace_id = %s"
            params += (str(workspace_id),)
        with self._pool.connection() as connection:
            rows = connection.execute(query + " ORDER BY record_id", params).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def list_by_field_in(
        self,
        field: str,
        values: tuple[str, ...],
        *,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> tuple[TContract, ...]:
        """Return a tenant's records whose field equals any of ``values``."""
        field = self._checked_field(field)
        if not values:
            return ()
        query = f"""SELECT payload FROM product_records
                        WHERE collection = %s AND org_id = %s
                          AND payload ->> '{field}' = ANY(%s)"""
        params: tuple[object, ...] = (self._collection, str(org_id), list(values))
        if workspace_id is not None:
            query = query.replace("org_id = %s", "org_id = %s AND workspace_id = %s")
            params = (self._collection, str(org_id), str(workspace_id), list(values))
        with self._pool.connection() as connection:
            rows = connection.execute(query + " ORDER BY record_id", params).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def find_scoped_by_field(
        self,
        field: str,
        value: str,
        *,
        org_id: OpaqueId,
        casefold: bool = False,
        exclude_status: str | None = None,
    ) -> TContract | None:
        """Return one record of an organization whose field equals ``value``.

        ``exclude_status`` repeats a partial unique index's predicate (for
        example ``deleted`` users) so the lookup is a single index probe.
        """
        field = self._checked_field(field)
        match = (
            f"lower(payload ->> '{field}') = lower(%s)"
            if casefold
            else f"payload ->> '{field}' = %s"
        )
        params: tuple[object, ...] = (self._collection, str(org_id), value)
        if exclude_status is not None:
            match += " AND payload->>'status' <> %s"
            params += (exclude_status,)
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""SELECT payload FROM product_records
                        WHERE collection = %s AND org_id = %s AND {match}
                        ORDER BY record_id LIMIT 1""",
                params,
            ).fetchone()
        return self._deserialize(row[0]) if row is not None else None

    def claim_next_queued(
        self,
        worker_id: str,
        timestamp: datetime,
        org_id: str | None,
        workspace_id: str | None,
    ) -> TContract | None:
        """Atomically claim the next eligible queued execution record."""
        scope_filter = ""
        scope_params: tuple[object, ...] = ()
        if org_id is not None:
            scope_filter += " AND org_id = %s"
            scope_params += (org_id,)
        if workspace_id is not None:
            scope_filter += " AND workspace_id = %s"
            scope_params += (workspace_id,)
        with self._pool.connection() as connection:
            row = connection.execute(
                f"""WITH candidate AS (
                           SELECT record_id FROM product_records
                           WHERE collection = %s{scope_filter}
                             AND payload->>'status' = 'queued'
                           ORDER BY created_at, record_id
                           LIMIT 1 FOR UPDATE SKIP LOCKED
                       )
                       UPDATE product_records AS job
                       SET payload = job.payload || jsonb_build_object(
                               'status', 'running',
                               'attempts',
                               COALESCE((job.payload->>'attempts')::integer, 0) + 1,
                               'worker_id', %s::text,
                               'started_at', to_jsonb(%s::timestamptz),
                               'updated_at', to_jsonb(%s::timestamptz)
                           ),
                           updated_at = %s
                       FROM candidate
                       WHERE job.collection = %s AND job.record_id = candidate.record_id
                       RETURNING job.payload""",
                (
                    self._collection,
                    *scope_params,
                    worker_id,
                    timestamp,
                    timestamp,
                    timestamp,
                    self._collection,
                ),
            ).fetchone()
        return self._deserialize(row[0]) if row is not None else None
