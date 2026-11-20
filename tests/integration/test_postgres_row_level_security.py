"""Database row policies isolate tenants even when application code forgets filters."""

import psycopg
import pytest
from cryptography.fernet import Fernet

from infrastructure.database.store import ProductPostgresStore
from infrastructure.secrets.store import EncryptedPostgresSecretStore
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)

ROLE = "weaves_app"


@pytest.fixture
def store(isolated_postgres_url):
    # max_size=1 forces every transaction onto the same pooled connection.
    from infrastructure.database.pool import open_postgres_pool

    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=1)
    store = ProductPostgresStore(pool=pool, app_role=ROLE)
    with store.pool.connection() as connection:
        for org in ("org-a", "org-b"):
            connection.execute(
                "INSERT INTO product_records (collection, record_id, org_id, payload)"
                " VALUES ('agents', %s, %s, '{}'::jsonb), "
                "('users', %s, %s, '{}'::jsonb), "
                "('execution_jobs', %s, %s, '{}'::jsonb)",
                (
                    f"agent-{org}",
                    org,
                    f"user-{org}",
                    org,
                    f"job-{org}",
                    org,
                ),
            )
        connection.execute(
            "INSERT INTO product_records (collection, record_id, org_id, payload) "
            "VALUES ('organizations', 'org-a', NULL, '{}'::jsonb), "
            "('organizations', 'org-b', NULL, '{}'::jsonb)"
        )
    yield store
    store.close()
    require_tenant_context(False)


def _ids(store, collection=None):
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT record_id FROM product_records "
            "WHERE %s::text IS NULL OR collection = %s ORDER BY record_id",
            (collection, collection),
        ).fetchall()
    return [row[0] for row in rows]


def test_a_tenant_sees_only_its_own_rows_even_without_a_filter(store):
    token = set_request_identity("principal", "org-a", "ws")
    try:
        assert _ids(store) == ["agent-org-a", "job-org-a", "org-a", "user-org-a"]
    finally:
        reset_request_principal(token)
    token = set_request_identity("principal", "org-b", "ws")
    try:
        assert _ids(store) == ["agent-org-b", "job-org-b", "org-b", "user-org-b"]
    finally:
        reset_request_principal(token)


def test_foreign_rows_cannot_be_read_updated_deleted_or_written(store):
    token = set_request_identity("principal", "org-a", "ws")
    try:
        with store.connection() as connection:
            assert not connection.execute(
                "SELECT 1 FROM product_records WHERE org_id = 'org-b'"
            ).fetchall()
            assert (
                connection.execute(
                    "UPDATE product_records SET payload = '{\"x\":1}' "
                    "WHERE org_id = 'org-b'"
                ).rowcount
                == 0
            )
            assert (
                connection.execute(
                    "DELETE FROM product_records WHERE org_id = 'org-b'"
                ).rowcount
                == 0
            )
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            store.connection() as connection,
        ):
            connection.execute(
                "INSERT INTO product_records (collection, record_id, org_id, "
                "payload) VALUES ('agents', 'planted', 'org-b', '{}'::jsonb)"
            )
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            store.connection() as connection,  # cannot transfer ownership
        ):
            connection.execute(
                "UPDATE product_records SET org_id = 'org-b' "
                "WHERE record_id = 'agent-org-a'"
            )
    finally:
        reset_request_principal(token)
    with store.pool.connection() as connection:  # owner view: nothing changed
        assert (
            connection.execute(
                "SELECT count(*) FROM product_records WHERE org_id = 'org-b'"
            ).fetchone()[0]
            == 3
        )
        assert not connection.execute(
            "SELECT 1 FROM product_records WHERE record_id = 'planted'"
        ).fetchall()


def test_strict_processes_without_context_expose_no_tenant_rows(store):
    require_tenant_context(True)
    assert _ids(store) == []


def test_tenant_scope_never_survives_rollback_or_connection_reuse(store):
    token = set_request_identity("principal", "org-a", "ws")
    try:
        with pytest.raises(RuntimeError), store.transaction() as connection:
            connection.execute("SELECT 1")
            raise RuntimeError("abort mid-transaction")
    finally:
        reset_request_principal(token)
    require_tenant_context(True)
    assert _ids(store) == []  # the single pooled connection kept no org-a scope
    with store.pool.connection() as connection:
        assert connection.execute("SELECT current_user").fetchone()[0] != ROLE
        assert connection.execute(
            "SELECT current_setting('app.org_id', true)"
        ).fetchone()[0] in (None, "")


def test_system_scopes_are_narrow_cross_tenant_operations(store):
    require_tenant_context(True)
    with system_scope("auth"):
        assert _ids(store, "users") == ["user-org-a", "user-org-b"]
        assert _ids(store, "agents") == []  # auth never reads tenant content
        assert _ids(store, "execution_jobs") == []
    with system_scope("queue"):
        assert _ids(store, "execution_jobs") == ["job-org-a", "job-org-b"]
        assert _ids(store, "users") == []
    with system_scope("unknown-purpose"):
        assert _ids(store) == []


def test_secrets_are_isolated_by_row_policy_when_application_code_misses_filters(
    store,
):
    secrets = EncryptedPostgresSecretStore(store, Fernet.generate_key().decode())
    token = set_request_identity("principal", "org-a", "ws")
    try:
        ref = secrets.put("a-only", purpose="model_provider")
    finally:
        reset_request_principal(token)
    token = set_request_identity("principal", "org-b", "ws")
    try:
        with store.connection() as connection:  # no org predicate on purpose
            assert (
                connection.execute("SELECT count(*) FROM product_secrets").fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "DELETE FROM product_secrets WHERE secret_ref = %s", (ref,)
                ).rowcount
                == 0
            )
    finally:
        reset_request_principal(token)
    token = set_request_identity("principal", "org-a", "ws")
    try:
        assert secrets.get(ref) == "a-only"
    finally:
        reset_request_principal(token)
