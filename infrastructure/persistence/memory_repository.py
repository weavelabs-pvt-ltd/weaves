"""Thread-safe in-memory implementation of product repositories."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any, Generic

from application.ports.repository import TContract
from contracts.v1.base import OpaqueId


def _record_value(record: TContract, field: str) -> Any:
    """Read a field from one product contract with a runtime-selected schema."""
    return getattr(record, field)


class InMemoryRepository(Generic[TContract]):
    """A local repository keyed by a contract's explicit identifier field."""

    def __init__(self, id_field: str) -> None:
        """Initialize the adapter with its identifier or resource configuration."""
        self._id_field = id_field
        self._records: dict[str, TContract] = {}
        self._lock = RLock()

    def create(self, record: TContract) -> TContract:
        """Insert a new record and reject an existing identifier."""
        record_id = self._record_id(record)
        with self._lock:
            if record_id in self._records:
                raise ValueError(f"{self._id_field} already exists: {record_id}")
            self._records[record_id] = self._clone(record)
        return self._clone(record)

    def put(self, record: TContract) -> TContract:
        """Insert or replace the record with this identifier."""
        record_id = self._record_id(record)
        with self._lock:
            self._records[record_id] = self._clone(record)
        return self._clone(record)

    def get(self, record_id: OpaqueId) -> TContract:
        """Return one detached record by its identifier."""
        with self._lock:
            try:
                return self._clone(self._records[record_id])
            except KeyError as exc:
                raise KeyError(f"{self._id_field} not found: {record_id}") from exc

    def get_scoped(
        self,
        record_id: OpaqueId,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> TContract:
        """Resolve an object only when its stored tenant scope matches."""
        record = self.get(record_id)
        if getattr(record, "org_id", None) != org_id:
            raise KeyError(f"{self._id_field} not found in organization scope")
        if (
            workspace_id is not None
            and getattr(record, "workspace_id", None) != workspace_id
        ):
            raise KeyError(f"{self._id_field} not found in workspace scope")
        return record

    def delete(self, record_id: OpaqueId) -> None:
        """Remove the record with this identifier."""
        with self._lock:
            if record_id not in self._records:
                raise KeyError(f"{self._id_field} not found: {record_id}")
            del self._records[record_id]

    def consume(self, record_id: OpaqueId) -> TContract:
        """Atomically return and remove a one-time record."""
        with self._lock:
            try:
                record = self._records.pop(record_id)
            except KeyError as exc:
                raise KeyError(f"{self._id_field} not found: {record_id}") from exc
            return self._clone(record)

    def list(self) -> tuple[TContract, ...]:
        """Return all stored records as detached values."""
        with self._lock:
            return tuple(self._clone(record) for record in self._records.values())

    def list_due(self, due_before: datetime, *, limit: int) -> tuple[TContract, ...]:
        """Return due records ordered for bounded worker processing."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            due = [
                record
                for record in self._records.values()
                if getattr(getattr(record, "status", None), "value", None) == "active"
                and isinstance(getattr(record, "next_run_at", None), datetime)
                and _record_value(record, "next_run_at") <= due_before
            ]
            due.sort(
                key=lambda record: (
                    _record_value(record, "next_run_at"),
                    self._record_id(record),
                )
            )
            return tuple(self._clone(record) for record in due[:limit])

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
        if self._id_field != "audit_event_id":
            raise TypeError("audit event paging requires an audit event repository")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            records = tuple(self._records.values())
        filtered = (
            record
            for record in records
            if getattr(record, "org_id", None) == org_id
            and (
                workspace_id is None
                or getattr(record, "workspace_id", None) == workspace_id
            )
            and (action is None or getattr(record, "action", None) == action)
            and (
                target_type is None
                or getattr(record, "target_type", None) == target_type
            )
            and (target_id is None or getattr(record, "target_id", None) == target_id)
            and (
                actor_principal_id is None
                or getattr(record, "actor_principal_id", None) == actor_principal_id
            )
            and (
                created_after is None
                or _record_value(record, "created_at") >= created_after
            )
            and (
                created_before is None
                or _record_value(record, "created_at") <= created_before
            )
            and (
                cursor_created_at is None
                or (
                    _record_value(record, "created_at"),
                    self._record_id(record),
                )
                < (cursor_created_at, cursor_event_id or "")
            )
        )
        ordered = sorted(
            filtered,
            key=lambda record: (
                _record_value(record, "created_at"),
                self._record_id(record),
            ),
            reverse=True,
        )
        return tuple(self._clone(record) for record in ordered[:limit])

    def find_by_field(self, field: str, value: str) -> TContract | None:
        """Return the first record matching a validated contract field."""
        with self._lock:
            for record in self._records.values():
                if getattr(record, field, None) == value:
                    return self._clone(record)
        return None

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
    ) -> tuple[TContract, ...]:
        """Return a bounded newest-first page from the task collection."""
        if self._id_field != "task_id":
            raise TypeError("task paging requires a task repository")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            records = tuple(self._records.values())
        selected = [
            record
            for record in records
            if getattr(record, "org_id", None) == org_id
            and getattr(record, "workspace_id", None) == workspace_id
            and (status is None or _record_value(record, "status").value == status)
            and (agent_id is None or getattr(record, "agent_id", None) == agent_id)
            and (
                getattr(record, "audience", "workspace") != "private"
                or getattr(record, "audience_owner_principal_id", None)
                == visible_to_principal_id
            )
            and (
                created_by_principal_id is None
                or getattr(record, "created_by_principal_id", None)
                == created_by_principal_id
            )
            and (
                cursor_created_at is None
                or (
                    _record_value(record, "created_at"),
                    self._record_id(record),
                )
                < (cursor_created_at, cursor_task_id or "")
            )
        ]
        selected.sort(
            key=lambda record: (
                _record_value(record, "created_at"),
                self._record_id(record),
            ),
            reverse=True,
        )
        return tuple(self._clone(record) for record in selected[:limit])

    def list_task_messages_page(
        self,
        task_id: OpaqueId,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
        *,
        cursor_created_at: datetime | None,
        cursor_message_id: str | None,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return a bounded newest-first transcript page for one task."""
        if self._id_field != "message_id":
            raise TypeError("task message paging requires a message repository")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            records = tuple(self._records.values())
        selected = [
            record
            for record in records
            if getattr(record, "task_id", None) == task_id
            and getattr(record, "org_id", None) == org_id
            and getattr(record, "workspace_id", None) == workspace_id
            and (
                cursor_created_at is None
                or (
                    _record_value(record, "created_at"),
                    self._record_id(record),
                )
                < (cursor_created_at, cursor_message_id or "")
            )
        ]
        selected.sort(
            key=lambda record: (
                _record_value(record, "created_at"),
                self._record_id(record),
            ),
            reverse=True,
        )
        return tuple(self._clone(record) for record in selected[:limit])

    def list_status_updated_before(
        self,
        status: str,
        updated_before: datetime,
        *,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return bounded stale records for lease recovery."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            selected = [
                record
                for record in self._records.values()
                if getattr(getattr(record, "status", None), "value", None) == status
                and _record_value(record, "updated_at") <= updated_before
            ]
        selected.sort(
            key=lambda record: (
                _record_value(record, "updated_at"),
                self._record_id(record),
            )
        )
        return tuple(self._clone(record) for record in selected[:limit])

    def claim_next_queued(
        self,
        worker_id: str,
        timestamp: datetime,
        org_id: str | None,
        workspace_id: str | None,
    ) -> TContract | None:
        """Atomically claim the next eligible queued execution record."""
        with self._lock:
            queued = [
                record
                for record in self._records.values()
                if (org_id is None or getattr(record, "org_id", None) == org_id)
                and (
                    workspace_id is None
                    or getattr(record, "workspace_id", None) == workspace_id
                )
                if getattr(getattr(record, "status", None), "value", None) == "queued"
            ]
            if not queued:
                return None
            current = min(queued, key=lambda item: _record_value(item, "created_at"))
            status_type = type(_record_value(current, "status"))
            claimed = type(current).model_validate(
                current.model_copy(
                    update={
                        "status": status_type("running"),
                        "attempts": int(getattr(current, "attempts", 0)) + 1,
                        "worker_id": worker_id,
                        "started_at": timestamp,
                        "updated_at": timestamp,
                    }
                ).model_dump()
            )
            self._records[self._record_id(current)] = self._clone(claimed)
            return self._clone(claimed)

    def list_scoped(
        self, org_id: OpaqueId, workspace_id: OpaqueId | None = None
    ) -> tuple[TContract, ...]:
        """List only records matching the requested organization/workspace."""
        with self._lock:
            return tuple(
                self._clone(record)
                for record in self._records.values()
                if getattr(record, "org_id", None) == org_id
                and (
                    workspace_id is None
                    or getattr(record, "workspace_id", None) == workspace_id
                )
            )

    def find_by_idempotency_key(
        self,
        key: str,
        requested_by_principal_id: str,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
    ) -> TContract | None:
        """Find a request record by its principal-scoped idempotency key."""
        with self._lock:
            for record in self._records.values():
                if (
                    getattr(record, "idempotency_key", None) == key
                    and getattr(record, "requested_by_principal_id", None)
                    == requested_by_principal_id
                    and getattr(record, "org_id", None) == org_id
                    and getattr(record, "workspace_id", None) == workspace_id
                ):
                    return self._clone(record)
        return None

    def put_if_status_and_stale(
        self,
        record: TContract,
        *,
        expected_status: str,
        updated_before: datetime,
    ) -> bool:
        """Replace a stale record only when its current status still matches."""
        record_id = self._record_id(record)
        with self._lock:
            current = self._records.get(record_id)
            current_status = getattr(current, "status", None)
            if getattr(current_status, "value", current_status) != expected_status:
                return False
            if getattr(current, "updated_at", updated_before) > updated_before:
                return False
            self._records[record_id] = self._clone(record)
        return True

    def put_if_execution_job_owned(
        self,
        record: TContract,
        *,
        worker_id: str,
        attempts: int,
        expected_status: str = "running",
    ) -> bool:
        """Update an execution job only for its current owner and state."""
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
        """Fence a record update to the worker's current lease and attempt."""
        record_id = self._record_id(record)
        with self._lock:
            current = self._records.get(record_id)
            if (
                current is None
                or getattr(getattr(current, "status", None), "value", None)
                != expected_status
                or getattr(current, "worker_id", None) != worker_id
                or getattr(current, "attempts", None) != attempts
            ):
                return False
            self._records[record_id] = self._clone(record)
        return True

    def put_if_task_state(
        self,
        record: TContract,
        *,
        expected_status: str,
        expected_active_turn_id: str | None,
    ) -> bool:
        """Compare task lifecycle and active turn before replacing the record."""
        record_id = self._record_id(record)
        with self._lock:
            current = self._records.get(record_id)
            if (
                current is None
                or getattr(getattr(current, "status", None), "value", None)
                != expected_status
                or getattr(current, "active_turn_id", None) != expected_active_turn_id
            ):
                return False
            self._records[record_id] = self._clone(record)
        return True

    def _record_id(self, record: TContract) -> str:
        """Read and normalize the contract identifier configured for this repository."""
        try:
            return str(getattr(record, self._id_field))
        except AttributeError as exc:
            raise TypeError(f"record has no {self._id_field} field") from exc

    def _clone(self, record: TContract) -> TContract:
        """Round-trip through the contract serializer for a detached deep copy."""
        return type(record).model_validate_json(record.model_dump_json())
