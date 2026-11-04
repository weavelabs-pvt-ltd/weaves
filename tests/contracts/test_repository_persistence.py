from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg.errors
import pytest

from contracts.v1 import (
    AuditEvent,
    ErrorSummary,
    ExecutionJob,
    ExecutionJobStatus,
    RunStatus,
    UserInvitation,
    UserInvitationStatus,
    WorkflowRun,
    WorkflowSchedule,
)
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.repositories import (
    IdempotencyKeyAlreadyExists,
    PostgresRepository,
)


class RecordingConnection:
    def __init__(self, rows=()):
        self.statements = []
        self.rowcount = 1
        self.rows = list(rows)

    def execute(self, query, params=None):
        if params is not None:
            assert query.count("%s") == len(params)
        self.statements.append((" ".join(query.split()), params))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return self.rows


class RecordingPool:
    def __init__(self, connection):
        self._connection = connection
        self.entry_count = 0

    @contextmanager
    def connection(self):
        self.entry_count += 1
        yield self._connection


def test_product_postgres_migration_adds_idempotency_unique_index():
    connection = RecordingConnection()
    store = ProductPostgresStore.__new__(ProductPostgresStore)
    store.pool = RecordingPool(connection)

    store._migrate()

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "ADD COLUMN IF NOT EXISTS idempotency_key text" in sql
    assert "payload->>'requested_by_principal_id'" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS product_records_run_idempotency_uq" in sql
    assert "WHERE collection = 'workflow_runs' AND idempotency_key IS NOT NULL" in sql
    assert "product_records_api_token_digest_uq" in sql
    assert "product_records_execution_job_idempotency_uq" in sql
    assert "product_records_execution_job_claim_idx" in sql
    assert "product_records_role_binding_scope_uq" in sql
    assert "COALESCE(payload->>'workspace_id', '')" in sql
    assert "product_records_user_session_digest_uq" in sql
    assert "product_records_user_invitation_digest_uq" in sql
    assert "product_records_active_user_email_uq" in sql
    assert "product_records_workflow_trigger_secret_uq" in sql
    assert "product_records_workflow_schedule_due_idx" in sql
    versions = [
        params[0]
        for statement, params in connection.statements
        if "INSERT INTO product_schema_migrations" in statement
    ]
    assert "product_records_audit_event_org_page_idx" in sql
    assert "product_records_audit_event_workspace_page_idx" in sql
    assert "IS DISTINCT FROM" in sql
    assert "product_records_task_idempotency_uq" in sql
    assert "product_records_task_turn_claim_idx" in sql
    assert versions == list(range(1, 15))
    assert "CREATE TABLE IF NOT EXISTS product_login_throttle" in sql
    assert "pg_advisory_xact_lock" in sql


def test_product_postgres_migration_skips_applied_versions():
    connection = RecordingConnection(rows=[(version,) for version in range(1, 15)])
    store = ProductPostgresStore.__new__(ProductPostgresStore)
    store.pool = RecordingPool(connection)

    store._migrate()

    assert not any(
        "CREATE TABLE IF NOT EXISTS product_records" in statement
        for statement, _ in connection.statements
    )
    assert not any(
        "INSERT INTO product_schema_migrations" in statement
        for statement, _ in connection.statements
    )


def test_product_postgres_migration_rejects_newer_database_schema():
    connection = RecordingConnection(rows=[(15,)])
    store = ProductPostgresStore.__new__(ProductPostgresStore)
    store.pool = RecordingPool(connection)

    with pytest.raises(RuntimeError, match="unsupported migration versions: 15"):
        store._migrate()


def test_product_postgres_migration_upgrades_legacy_version_five():
    connection = RecordingConnection(rows=[(version,) for version in range(1, 6)])
    store = ProductPostgresStore.__new__(ProductPostgresStore)
    store.pool = RecordingPool(connection)

    store._migrate()

    sql = "\n".join(statement for statement, _ in connection.statements)
    versions = [
        params[0]
        for statement, params in connection.statements
        if "INSERT INTO product_schema_migrations" in statement
    ]
    assert versions == list(range(6, 15))
    assert "product_records_user_session_digest_uq" in sql
    assert "product_records_workflow_schedule_due_idx" in sql
    assert "product_records_audit_event_workspace_page_idx" in sql


def test_postgres_execution_job_updates_are_fenced_by_worker_and_attempt():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection), "execution_jobs", "job_id", ExecutionJob
    )
    now = datetime.now(UTC)
    job = ExecutionJob(
        job_id="job-a",
        org_id="org-a",
        workspace_id="workspace-a",
        requested_by_principal_id="principal-a",
        task="Run the requested task",
        status=ExecutionJobStatus.RUNNING,
        attempts=2,
        worker_id="worker-a",
        started_at=now,
        created_at=now,
        updated_at=now,
    )

    assert repository.put_if_execution_job_owned(job, worker_id="worker-a", attempts=2)
    query, params = connection.statements[-1]
    assert "payload->>'status' = %s" in query
    assert "payload->>'worker_id' = %s" in query
    assert "(payload->>'attempts')::integer = %s" in query
    assert params[1] == job.updated_at
    assert params[2:4] == ("execution_jobs", job.job_id)
    assert params[4] == "running"
    assert params[-2:] == ("worker-a", 2)


def test_postgres_invitation_transition_is_fenced_by_pending_token():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection), "user_invitations", "invitation_id", UserInvitation
    )
    now = datetime.now(UTC)
    invitation = UserInvitation(
        invitation_id="invite-a",
        org_id="org-a",
        workspace_id="workspace-a",
        user_id="user-a",
        principal_id="principal-a",
        email="person@example.com",
        token_digest="a" * 64,
        status=UserInvitationStatus.PENDING,
        expires_at=now + timedelta(days=7),
        created_at=now,
        updated_at=now,
    )

    assert repository.put_if_invitation_pending(
        invitation, expected_token_digest="a" * 64
    )
    query, params = connection.statements[-1]
    assert "payload->>'status' = 'pending'" in query
    assert "payload->>'token_digest' = %s" in query
    assert params[1] == invitation.updated_at
    assert params[2:4] == ("user_invitations", invitation.invitation_id)
    assert params[-1] == "a" * 64


def test_postgres_store_reuses_one_connection_inside_unit_of_work():
    connection = RecordingConnection()
    pool = RecordingPool(connection)
    store = ProductPostgresStore.__new__(ProductPostgresStore)
    store.pool = pool
    store._active_connection = ContextVar("test_product_connection", default=None)

    with store.transaction():
        store.acquire_advisory_transaction_lock("workspace-lifecycle:org-a")
        with store.connection() as first:
            first.execute("SELECT 1")
        with store.transaction(), store.connection() as second:
            second.execute("SELECT 2")

    assert first is second is connection
    assert pool.entry_count == 1
    assert any(
        "pg_advisory_xact_lock(hashtextextended(%s, 0))" in statement
        for statement, _ in connection.statements
    )


def test_postgres_run_repository_reports_idempotency_index_conflict(monkeypatch):
    class UniqueViolation(Exception):
        def __init__(self):
            self.diag = SimpleNamespace(
                constraint_name="product_records_run_idempotency_uq"
            )

    class FailingConnection:
        def execute(self, query, params=None):
            raise UniqueViolation()

    monkeypatch.setattr(psycopg.errors, "UniqueViolation", UniqueViolation)
    repository = PostgresRepository(
        RecordingPool(FailingConnection()),
        "workflow_runs",
        "run_id",
        WorkflowRun,
    )
    now = datetime.now(UTC)
    run = WorkflowRun(
        run_id="run-1",
        org_id="local-org",
        workspace_id="local-workspace",
        workflow_id="workflow-1",
        workflow_version_id="workflow-version-1",
        requested_by_principal_id="local-developer",
        trigger_type="local.runtime",
        idempotency_key="request-1",
        status=RunStatus.RUNNING,
        started_at=now,
        created_at=now,
        updated_at=now,
    )

    with pytest.raises(IdempotencyKeyAlreadyExists):
        repository.create(run)


def test_postgres_repository_looks_up_credentials_by_indexed_digest():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection),
        "api_credentials",
        "id",
        WorkflowRun,
    )

    assert repository.find_by_field("token_digest", "a" * 64) is None
    statement, params = connection.statements[0]
    assert "payload ->> 'token_digest' = %s" in statement
    assert params == ("api_credentials", "a" * 64)


def test_postgres_repository_rejects_untrusted_lookup_field_names():
    repository = PostgresRepository(
        RecordingPool(RecordingConnection()),
        "api_credentials",
        "id",
        WorkflowRun,
    )
    with pytest.raises(ValueError, match="valid contract field"):
        repository.find_by_field("token_digest OR TRUE", "a" * 64)


def test_postgres_workflow_schedule_lookup_is_indexable_and_bounded():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection),
        "workflow_schedules",
        "schedule_id",
        WorkflowSchedule,
    )
    timestamp = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    assert repository.list_due(timestamp, limit=25) == ()
    statement, params = connection.statements[0]
    assert "payload->>'status' = 'active'" in statement
    assert "payload->>'next_run_at' <= %s" in statement
    assert "ORDER BY payload->>'next_run_at', record_id" in statement
    assert "LIMIT %s" in statement
    assert params == ("workflow_schedules", "2026-01-02T03:04:00Z", 25)


def test_postgres_audit_page_uses_scoped_keyset_query():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection), "audit_events", "audit_event_id", AuditEvent
    )
    created_after = datetime(2026, 1, 1, tzinfo=UTC)
    cursor_created_at = datetime(2026, 1, 2, tzinfo=UTC)

    assert (
        repository.list_audit_page(
            "org-a",
            "workspace-a",
            action="workflow.run.succeeded",
            target_type="workflow",
            target_id=None,
            actor_principal_id="principal-a",
            created_after=created_after,
            created_before=None,
            cursor_created_at=cursor_created_at,
            cursor_event_id="event-a",
            limit=51,
        )
        == ()
    )
    statement, params = connection.statements[0]
    assert "org_id = %s AND workspace_id = %s" in statement
    assert "payload->>'action' = %s" in statement
    assert "payload->>'target_type' = %s" in statement
    assert "payload->>'actor_principal_id' = %s" in statement
    assert "(created_at, record_id) < (%s, %s)" in statement
    assert "ORDER BY created_at DESC, record_id DESC LIMIT %s" in statement
    assert params == (
        "audit_events",
        "org-a",
        "workspace-a",
        "workflow.run.succeeded",
        "workflow",
        "principal-a",
        created_after,
        cursor_created_at,
        "event-a",
        51,
    )


def test_postgres_execution_job_claim_uses_skip_locked_atomic_update():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection), "execution_jobs", "job_id", ExecutionJob
    )
    timestamp = datetime.now(UTC)

    assert (
        repository.claim_next_queued("worker-a", timestamp, "org-a", "workspace-a")
        is None
    )
    statement, params = connection.statements[0]
    assert "LIMIT 1 FOR UPDATE SKIP LOCKED" in statement
    assert "payload->>'status' = 'queued'" in statement
    assert "RETURNING job.payload" in statement
    assert params == (
        "execution_jobs",
        "org-a",
        "workspace-a",
        "worker-a",
        timestamp,
        timestamp,
        timestamp,
        "execution_jobs",
    )


def test_postgres_execution_worker_can_claim_across_tenant_scopes():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection), "execution_jobs", "job_id", ExecutionJob
    )

    assert (
        repository.claim_next_queued("worker-global", datetime.now(UTC), None, None)
        is None
    )
    statement, params = connection.statements[0]
    assert "LIMIT 1 FOR UPDATE SKIP LOCKED" in statement
    assert "payload->>'status' = 'queued'" in statement
    assert "AND org_id =" not in statement
    assert "AND workspace_id =" not in statement
    assert params[0:2] == ("execution_jobs", "worker-global")
    assert len(params) == 6
    assert params[-1] == "execution_jobs"


def test_postgres_repository_recovers_only_stale_running_status():
    connection = RecordingConnection()
    repository = PostgresRepository(
        RecordingPool(connection),
        "workflow_runs",
        "run_id",
        WorkflowRun,
    )
    now = datetime.now(UTC)
    run = WorkflowRun(
        run_id="run-stale",
        org_id="local-org",
        workspace_id="local-workspace",
        workflow_id="workflow-1",
        workflow_version_id="workflow-version-1",
        requested_by_principal_id="local-developer",
        trigger_type="local.runtime",
        status=RunStatus.FAILED,
        started_at=now - timedelta(hours=5),
        finished_at=now,
        error=ErrorSummary(
            code="runtime.interrupted",
            summary="Execution heartbeat is stale.",
            retryable=True,
        ),
        created_at=now - timedelta(hours=5),
        updated_at=now,
    )

    updated = repository.put_if_status_and_stale(
        run,
        expected_status=RunStatus.RUNNING.value,
        updated_before=now - timedelta(hours=4),
    )

    statement, params = connection.statements[0]
    assert updated is True
    assert "payload->>'status' = %s AND updated_at <= %s" in statement
    assert params[1] == run.updated_at
    assert params[4] == RunStatus.RUNNING.value
    assert params[5] == now - timedelta(hours=4)
