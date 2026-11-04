"""PostgreSQL setup for the product runtime's versioned record store."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_MIGRATION_LOCK_NAME = "weaves-product-schema-migrations"


_SCHEMA_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """CREATE TABLE IF NOT EXISTS product_records (
                    collection text NOT NULL,
                    record_id text NOT NULL,
                    org_id text,
                    workspace_id text,
                    idempotency_key text,
                    requested_by_principal_id text,
                    payload jsonb NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (collection, record_id)
                )""",
            """ALTER TABLE product_records
                   ADD COLUMN IF NOT EXISTS idempotency_key text""",
            """ALTER TABLE product_records
                   ADD COLUMN IF NOT EXISTS requested_by_principal_id text""",
            """UPDATE product_records
                   SET idempotency_key = payload->>'idempotency_key',
                       requested_by_principal_id = payload->>'requested_by_principal_id'
                   WHERE collection = 'workflow_runs'
                     AND idempotency_key IS NULL
                     AND payload->>'idempotency_key' IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS product_records_org_scope_idx
                   ON product_records (collection, org_id, record_id)""",
            """CREATE INDEX IF NOT EXISTS product_records_workspace_scope_idx
                   ON product_records (collection, org_id, workspace_id, record_id)""",
            """CREATE UNIQUE INDEX IF NOT EXISTS product_records_run_idempotency_uq
                   ON product_records
                     (org_id, workspace_id, requested_by_principal_id, idempotency_key)
                   WHERE collection = 'workflow_runs'
                     AND idempotency_key IS NOT NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS product_records_api_token_digest_uq
                   ON product_records (collection, (payload->>'token_digest'))
                   WHERE collection = 'api_credentials'""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_execution_job_idempotency_uq
                   ON product_records
                     (org_id, workspace_id, requested_by_principal_id, idempotency_key)
                   WHERE collection = 'execution_jobs'
                     AND idempotency_key IS NOT NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_role_binding_scope_uq
                   ON product_records
                     (org_id, (payload->>'principal_id'), (payload->>'role_id'),
                      (COALESCE(payload->>'workspace_id', '')))
                   WHERE collection = 'role_bindings'""",
            """CREATE INDEX IF NOT EXISTS product_records_execution_job_claim_idx
                   ON product_records (created_at, record_id)
                   WHERE collection = 'execution_jobs'
                     AND payload->>'status' = 'queued'""",
        ),
    ),
    (2, ()),
    (3, ()),
    (4, ()),
    (5, ()),
    (
        6,
        (
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_user_session_digest_uq
                   ON product_records (collection, (payload->>'token_digest'))
                   WHERE collection = 'user_sessions'""",
        ),
    ),
    (
        7,
        (
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_user_invitation_digest_uq
                   ON product_records (collection, (payload->>'token_digest'))
                   WHERE collection = 'user_invitations'""",
        ),
    ),
    (
        8,
        (
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_active_user_email_uq
                   ON product_records (org_id, lower(payload->>'email'))
                   WHERE collection = 'users' AND payload->>'status' <> 'deleted'""",
        ),
    ),
    (
        9,
        (
            """CREATE TABLE IF NOT EXISTS product_login_throttle (
                    bucket_key text PRIMARY KEY,
                    window_started_at timestamptz NOT NULL,
                    attempts integer NOT NULL CHECK (attempts > 0),
                    updated_at timestamptz NOT NULL
                )""",
            """CREATE INDEX IF NOT EXISTS product_login_throttle_window_idx
                   ON product_login_throttle (window_started_at)""",
        ),
    ),
    (
        10,
        (
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_workflow_trigger_secret_uq
                   ON product_records (collection, (payload->>'secret_digest'))
                   WHERE collection = 'workflow_triggers'""",
        ),
    ),
    (
        11,
        (
            """CREATE INDEX IF NOT EXISTS product_records_workflow_schedule_due_idx
                   ON product_records ((payload->>'next_run_at'), record_id)
                   WHERE collection = 'workflow_schedules'
                     AND payload->>'status' = 'active'""",
        ),
    ),
    (
        12,
        (
            """UPDATE product_records
                   SET created_at = (payload->>'created_at')::timestamptz
                   WHERE collection = 'audit_events'
                     AND payload->>'created_at' IS NOT NULL
                     AND created_at IS DISTINCT FROM
                         (payload->>'created_at')::timestamptz""",
            """CREATE INDEX IF NOT EXISTS product_records_audit_event_org_page_idx
                   ON product_records (org_id, created_at DESC, record_id DESC)
                   WHERE collection = 'audit_events'""",
            """CREATE INDEX IF NOT EXISTS
                   product_records_audit_event_workspace_page_idx
                   ON product_records
                     (org_id, workspace_id, created_at DESC, record_id DESC)
                   WHERE collection = 'audit_events'""",
        ),
    ),
    (
        13,
        (
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_system_role_key_uq
                   ON product_records (org_id, (payload->>'system_role_key'))
                   WHERE collection = 'roles'
                     AND payload->>'system_role_key' IS NOT NULL""",
        ),
    ),
    (
        14,
        (
            """CREATE UNIQUE INDEX IF NOT EXISTS product_records_task_idempotency_uq
                   ON product_records
                     (org_id, workspace_id, requested_by_principal_id, idempotency_key)
                   WHERE collection = 'tasks'
                     AND idempotency_key IS NOT NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS
                   product_records_task_turn_idempotency_uq
                   ON product_records
                     (org_id, workspace_id, requested_by_principal_id, idempotency_key)
                   WHERE collection = 'task_turns'
                     AND idempotency_key IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS product_records_task_page_idx
                   ON product_records
                     (org_id, workspace_id, created_at DESC, record_id DESC)
                   WHERE collection = 'tasks'""",
            """CREATE INDEX IF NOT EXISTS product_records_task_messages_page_idx
                   ON product_records
                     (org_id, workspace_id, (payload->>'task_id'),
                      created_at DESC, record_id DESC)
                   WHERE collection = 'task_messages'""",
            """CREATE INDEX IF NOT EXISTS product_records_task_turn_claim_idx
                   ON product_records (created_at, record_id)
                   WHERE collection = 'task_turns'
                     AND payload->>'status' = 'queued'""",
            """CREATE INDEX IF NOT EXISTS product_records_task_turn_stale_idx
                   ON product_records (updated_at, record_id)
                   WHERE collection = 'task_turns'
                     AND payload->>'status' = 'running'""",
        ),
    ),
)


def _schema_migrations() -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Return ordered migrations, retaining the versions used by older releases.

    Versions 2–5 were recorded by the original bootstrap without separate DDL.
    They remain reserved so existing databases can be upgraded without
    rewriting migration history.
    """
    return _SCHEMA_MIGRATIONS


class ProductPostgresStore:
    """Applies ordered schema migrations and lends pooled connections."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        pool: Any | None = None,
        max_size: int = 10,
        application_name: str = "weaves-product-api",
    ) -> None:
        """Open or accept a pool, then apply migrations before serving traffic."""
        if pool is None:
            if database_url is None or not database_url.strip():
                raise ValueError(
                    "A database URL or an open connection pool is required"
                )
            from infrastructure.database.pool import open_postgres_pool

            pool = open_postgres_pool(
                database_url.strip(),
                max_size=max_size,
                application_name=application_name,
            )
        self.pool: Any = pool
        self._active_connection: ContextVar[Any] = ContextVar(
            f"weaves_postgres_connection_{id(self)}", default=None
        )
        try:
            self._migrate()
        except Exception:
            self.close()
            raise

    def _migrate(self) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS product_schema_migrations (
                    version integer PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (_MIGRATION_LOCK_NAME,),
            )
            applied = {
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM product_schema_migrations ORDER BY version"
                ).fetchall()
            }
            migrations = _schema_migrations()
            unknown_versions = applied.difference(version for version, _ in migrations)
            if unknown_versions:
                raise RuntimeError(
                    "Database schema contains unsupported migration versions: "
                    + ", ".join(str(version) for version in sorted(unknown_versions))
                )
            for version, statements in migrations:
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO product_schema_migrations (version) VALUES (%s)",
                    (version,),
                )

    def close(self) -> None:
        """Close the connection pool. A second call is a no-op."""
        pool = self.pool
        self.pool = None
        if pool is not None:
            pool.close()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Reuse the active unit-of-work connection or borrow a fresh one."""
        active = self._active_connection.get()
        if active is not None:
            yield active
            return
        with self.pool.connection() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Group repository calls on one connection and commit them together."""
        active = self._active_connection.get()
        if active is not None:
            yield active
            return
        with self.pool.connection() as connection:
            token = self._active_connection.set(connection)
            try:
                yield connection
            finally:
                self._active_connection.reset(token)

    def acquire_advisory_transaction_lock(self, lock_name: str) -> None:
        """Serialize a cross-record invariant for this active transaction."""
        connection = self._active_connection.get()
        if connection is None:
            raise RuntimeError("an advisory lock requires an active transaction")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_name,)
        )

    def check(self) -> None:
        """Fail if the configured database cannot serve a simple query."""
        with self.pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()
