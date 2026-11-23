"""Generic persistence contract used by product application capabilities."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypeVar

from application.ports.fair_claim import FairClaim
from application.ports.limits import LimitPolicy
from contracts.v1.base import OpaqueId, ProductContract

TContract = TypeVar("TContract", bound=ProductContract)


def _updated_at(record: ProductContract) -> datetime:
    """Return a mutable record timestamp for compare-and-set updates."""
    value = getattr(record, "updated_at", None)
    if not isinstance(value, datetime):
        raise TypeError("conditional updates require a mutable record with updated_at")
    return value


class Repository(Protocol[TContract]):
    """Persistence operations shared by in-memory and database adapters."""

    def create(self, record: TContract) -> TContract:
        """Insert a new record and reject an existing identifier."""
        ...

    def put(self, record: TContract) -> TContract:
        """Insert or replace the record with the same identifier."""
        ...

    def get(self, record_id: OpaqueId) -> TContract:
        """Return a record by identifier or raise when it is missing."""
        ...

    def get_scoped(
        self,
        record_id: OpaqueId,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> TContract:
        """Return a record only within the requested tenant scope."""
        ...

    def delete(self, record_id: OpaqueId) -> None:
        """Remove a record by identifier."""
        ...

    def consume(self, record_id: OpaqueId) -> TContract:
        """Atomically return and remove a one-time record."""
        ...

    def list(self) -> tuple[TContract, ...]:
        """Return all records as detached values."""
        ...

    def list_due(self, due_before: datetime, *, limit: int) -> tuple[TContract, ...]:
        """Return due records in order, bounded by the requested page size."""
        ...

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
        """Return one filtered audit page after an optional keyset cursor."""
        ...

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
        """Return one bounded task page in newest-first keyset order."""
        ...

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
        """Return one newest-first page of a task's scoped messages."""
        ...

    def list_status_updated_before(
        self,
        status: str,
        updated_before: datetime,
        *,
        limit: int,
    ) -> tuple[TContract, ...]:
        """Return a bounded set of records with a stale status lease."""
        ...

    def list_by_status(
        self, status: str, *, limit: int, after_id: str | None = None
    ) -> tuple[TContract, ...]:
        """Return a bounded, identifier-ordered page of records in one status."""
        ...

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

        ``filters`` are equality predicates on contract fields. Order is
        ``(created_at, record_id)`` descending.
        """
        ...

    def get_many_scoped(
        self,
        record_ids: tuple[str, ...],
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> tuple[TContract, ...]:
        """Return the tenant's records among ``record_ids`` (missing ones skipped)."""
        ...

    def list_by_field_in(
        self,
        field: str,
        values: tuple[str, ...],
        *,
        org_id: OpaqueId,
        workspace_id: OpaqueId | None = None,
    ) -> tuple[TContract, ...]:
        """Return a tenant's records whose field equals any of ``values``."""
        ...

    def find_scoped_by_field(
        self,
        field: str,
        value: str,
        *,
        org_id: OpaqueId,
        casefold: bool = False,
        exclude_status: str | None = None,
    ) -> TContract | None:
        """Return one record of an organization whose field equals ``value``."""
        ...

    def find_by_field(self, field: str, value: str) -> TContract | None:
        """Find the first record matching a validated contract field."""
        ...

    def list_by_field(
        self, field: str, value: str, *, casefold: bool = False
    ) -> tuple[TContract, ...]:
        """Return every record whose field equals the value, ordered by identifier.

        With ``casefold`` the comparison ignores letter case, for normalized
        lookups such as email addresses.
        """
        ...

    def list_before(
        self,
        field: str,
        before: datetime,
        *,
        limit: int,
        equals: dict[str, str] | None = None,
    ) -> tuple[TContract, ...]:
        """Return up to ``limit`` records whose timestamp field is before a cutoff.

        ``equals`` adds equality predicates on other fields (booleans compare as
        ``true``/``false``).

        Records with no value for the field are skipped. Order is the field
        value, then identifier, so repeated bounded calls make progress.
        """
        ...

    def claim_next_queued(
        self,
        worker_id: str,
        timestamp: datetime,
        org_id: str | None,
        workspace_id: str | None,
    ) -> TContract | None:
        """Atomically claim the next eligible queued record."""
        ...

    def claim_next_fair(
        self, worker_id: str, timestamp: datetime, limits: LimitPolicy
    ) -> FairClaim[TContract] | None:
        """Claim the next queued record fairly across organizations.

        Organizations are served least recently claimed first, and an
        organization already running ``max_running_per_org`` rows (jobs and
        task turns together) is skipped while limits are enforced.
        """
        ...

    def oldest_queued_at(self) -> datetime | None:
        """Return the creation time of the oldest queued record, or None."""
        ...

    def prune_claim_state(self, claimed_before: datetime) -> int:
        """Forget round-robin history older than a cutoff; return rows removed."""
        ...

    def list_scoped(
        self, org_id: OpaqueId, workspace_id: OpaqueId | None = None
    ) -> tuple[TContract, ...]:
        """Return records visible within the requested tenant scope."""
        ...

    def find_by_idempotency_key(
        self,
        key: str,
        requested_by_principal_id: str,
        org_id: OpaqueId,
        workspace_id: OpaqueId,
    ) -> TContract | None:
        """Find a request record by its principal-scoped idempotency key."""
        ...

    def put_if_status_and_stale(
        self,
        record: TContract,
        *,
        expected_status: str,
        updated_before: datetime,
    ) -> bool:
        """Replace a record only if status matches and it is stale."""
        ...

    def put_if_execution_job_owned(
        self,
        record: TContract,
        *,
        worker_id: str,
        attempts: int,
        expected_status: str = "running",
    ) -> bool:
        """Update a queued job only for the current worker lease."""
        ...

    def put_if_worker_owned(
        self,
        record: TContract,
        *,
        worker_id: str,
        attempts: int,
        expected_status: str = "running",
    ) -> bool:
        """Fence a worker update to the record's current lease and attempt."""
        ...

    def put_if_task_state(
        self,
        record: TContract,
        *,
        expected_status: str,
        expected_active_turn_id: str | None,
    ) -> bool:
        """Compare and set a task only while its lifecycle/active turn match."""
        ...
