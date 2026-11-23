"""Audit repositories that stamp the active request id as ``correlation_id``.

Audit events keep their own ``request_id`` (it names the changed record in many
call sites). The correlation id is the originating HTTP request, bound for API
calls and carried to the worker through the job or turn, so one browser request
can be followed from its API log line to the audit trail of the run it started.
"""

from __future__ import annotations

from contracts.v1 import AuditEvent
from infrastructure.persistence.memory_repository import InMemoryRepository
from infrastructure.persistence.postgres_repository import PostgresRepository
from shared.correlation import current_request_id


def correlate(record: AuditEvent) -> AuditEvent:
    """Fill ``correlation_id`` from the active request when it is unset."""
    request_id = current_request_id()
    if request_id is None or record.correlation_id is not None:
        return record
    return record.model_copy(update={"correlation_id": request_id})


class CorrelatedInMemoryAudit(InMemoryRepository[AuditEvent]):
    """In-memory audit repository with request correlation."""

    def create(self, record: AuditEvent) -> AuditEvent:
        """Insert the event with the active request id as its correlation id."""
        return super().create(correlate(record))


class CorrelatedPostgresAudit(PostgresRepository[AuditEvent]):
    """PostgreSQL audit repository with request correlation."""

    def create(self, record: AuditEvent) -> AuditEvent:
        """Insert the event with the active request id as its correlation id."""
        return super().create(correlate(record))
