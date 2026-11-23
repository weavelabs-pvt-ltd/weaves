"""The public waitlist on PostgreSQL under the restricted application role."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import psycopg
import pytest

from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database import store as store_module
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.row_security import WAITLIST_COLLECTIONS
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from infrastructure.database.waitlist_schema import WAITLIST_MIGRATION
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.operator_scripts import run_script, waitlist
from tests.support.waitlist_world import URL, answer, client_for, join, token_headers

EMAIL = "Founder@Acme-Corp.test"
OTHER_PURPOSES = (
    "bootstrap",
    "auth",
    "queue",
    "dispatch",
    "recovery",
    "studio",
    "account",
    "lifecycle",
    "catalog",
    "oauth",
)


def open_runtime(url, role):
    pool = open_postgres_pool(url, min_size=1, max_size=8)
    store = ProductPostgresStore(pool=pool, app_role=role)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


@pytest.fixture
def stack(isolated_postgres_url):
    owner = open_runtime(isolated_postgres_url, "")
    api = open_runtime(isolated_postgres_url, "weaves_app")
    require_tenant_context(True)  # production posture: no implicit local tenant
    yield {"owner": owner, "api": api, "url": isolated_postgres_url}
    require_tenant_context(False)
    api.close()
    owner.close()


def sql(stack, query, *params):
    with psycopg.connect(stack["url"], autocommit=True) as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def waitlist_rows(stack):
    return sql(
        stack,
        "SELECT collection, org_id, payload::text FROM product_records "
        "WHERE collection = ANY(%s) ORDER BY collection, record_id",
        list(WAITLIST_COLLECTIONS),
    )


def test_public_flow_stores_global_rows_and_only_a_token_digest(stack):
    client = client_for(stack["api"])
    body = join(client, EMAIL, source={"page": "/"}).json()
    token = body["token"]
    assert answer(client, token, "company_location", "india").status_code == 200
    assert (
        answer(client, token, "tools", ["slack", "other"], other="Linear").status_code
        == 200
    )
    resumed = client.get(f"{URL}/submissions/current", headers=token_headers(token))
    assert resumed.json()["answers"]["company_location"] == {"value": "india"}
    cleared = client.delete(
        f"{URL}/submissions/current/answers/tools", headers=token_headers(token)
    )
    assert set(cleared.json()["answers"]) == {"company_location"}
    rows = waitlist_rows(stack)
    assert [r[0] for r in rows] == ["waitlist_entries", "waitlist_submissions"]
    assert all(r[1] is None for r in rows)
    assert token not in "".join(r[2] for r in rows)
    assert hashlib.sha256(token.encode()).hexdigest() in rows[1][2]
    assert "founder@acme-corp.test" in rows[0][2] and EMAIL not in rows[0][2]
    for collection in ("accounts", "account_sessions", "organizations", "users"):
        assert not sql(
            stack, "SELECT 1 FROM product_records WHERE collection = %s", collection
        )
    assert "set-cookie" not in resumed.headers


def test_known_email_and_idempotent_retry_on_postgres(stack):
    client = client_for(stack["api"])
    one = join(client, EMAIL, key="k-1").json()
    answer(client, one["token"], "role", "finance")
    retry = join(client, EMAIL, key="k-1")
    assert retry.status_code == 201
    second = join(client_for(stack["api"], "198.51.100.9"), EMAIL, key="k-2").json()
    assert second["answers"] == {} and second["token"] != one["token"]
    entries = [r for r in waitlist_rows(stack) if r[0] == "waitlist_entries"]
    submissions = [r for r in waitlist_rows(stack) if r[0] == "waitlist_submissions"]
    assert len(entries) == 1 and len(submissions) == 2
    assert json.loads(entries[0][2])["submission_count"] == 2


def test_concurrent_joins_of_one_email_make_one_entry(stack):
    clients = [client_for(stack["api"], f"198.18.1.{index}") for index in range(4)]

    def attempt(index):
        return join(clients[index], EMAIL, key=f"race-{index}").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(attempt, range(4))) == [201] * 4
    entries = [r for r in waitlist_rows(stack) if r[0] == "waitlist_entries"]
    assert len(entries) == 1
    assert json.loads(entries[0][2])["submission_count"] == 4
    assert len(waitlist_rows(stack)) == 5


def test_the_database_itself_refuses_duplicate_emails_and_tokens(stack):
    join(client_for(stack["api"]), EMAIL)
    entry, submission = (json.loads(r[2]) for r in waitlist_rows(stack))
    insert = (
        "INSERT INTO product_records (collection, record_id, org_id, payload) "
        "VALUES (%s, %s, NULL, %s::jsonb)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql(
            stack,
            insert,
            "waitlist_entries",
            "other-id",
            json.dumps({**entry, "email": EMAIL.upper()}),
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql(stack, insert, "waitlist_submissions", "other-id", json.dumps(submission))


@contextmanager
def scoped(store, *, org=None, purpose=None):
    token = set_request_identity("principal", org, "ws") if org else None
    try:
        if purpose:
            with system_scope(purpose):
                yield store
        else:
            yield store
    finally:
        if token is not None:
            reset_request_principal(token)


def visible(store, **scope):
    with scoped(store, **scope), store.connection() as connection:
        return connection.execute(
            "SELECT collection FROM product_records ORDER BY collection, record_id"
        ).fetchall()


def store_of(stack):
    """The restricted-role store behind the API runtime's repositories."""
    return stack["api"].waitlist_service.entries._pool


@pytest.fixture
def mixed(stack):
    """Waitlist rows beside tenant rows, a global row and a null-org row."""
    join(client_for(stack["api"]), EMAIL)
    sql(
        stack,
        "INSERT INTO product_records (collection, record_id, org_id, payload) VALUES "
        "('agents', 'agent-a', 'org-a', '{}'), ('plugins', 'plugin-1', NULL, '{}'), "
        "('organizations', 'org-a', NULL, '{}')",
    )


def test_no_tenant_and_no_other_purpose_can_read_waitlist_rows(stack, mixed):
    store = store_of(stack)
    tenant = {r[0] for r in visible(store, org="org-a")}
    assert {"agents", "plugins"} <= tenant  # the tenant and global rows it may see
    assert not tenant & set(WAITLIST_COLLECTIONS)
    for purpose in OTHER_PURPOSES:
        names = {r[0] for r in visible(store, purpose=purpose)}
        assert not names & set(WAITLIST_COLLECTIONS), purpose
    assert not [r for r in visible(store) if r[0] in WAITLIST_COLLECTIONS]


def test_no_tenant_and_no_other_purpose_can_write_waitlist_rows(stack, mixed):
    store = store_of(stack)
    for scope in ({"org": "org-a"}, *({"purpose": p} for p in OTHER_PURPOSES)):
        with scoped(store, **scope), store.connection() as connection:
            for statement in (
                "UPDATE product_records SET payload = '{}' "
                "WHERE collection LIKE 'waitlist%'",
                "DELETE FROM product_records WHERE collection LIKE 'waitlist%'",
            ):
                assert connection.execute(statement).rowcount == 0, scope
        with (
            pytest.raises(psycopg.Error),
            scoped(store, **scope),
            store.connection() as connection,
        ):
            connection.execute(
                "INSERT INTO product_records (collection, record_id, org_id, payload)"
                " VALUES ('waitlist_entries', 'forged', NULL, '{}'::jsonb)"
            )
    assert len(waitlist_rows(stack)) == 2


def test_the_waitlist_purpose_sees_only_the_two_waitlist_collections(stack, mixed):
    store = store_of(stack)
    names = {r[0] for r in visible(store, purpose="waitlist")}
    assert names == set(WAITLIST_COLLECTIONS)
    with scoped(store, org="org-a", purpose="waitlist"):
        assert {r[0] for r in visible(store, org="org-a", purpose="waitlist")} == set(
            WAITLIST_COLLECTIONS
        )
    with (
        pytest.raises(psycopg.Error),
        scoped(store, purpose="waitlist"),
        store.connection() as connection,
    ):
        connection.execute(
            "INSERT INTO product_records (collection, record_id, org_id, payload)"
            " VALUES ('agents', 'x', NULL, '{}'::jsonb)"
        )
    with scoped(store, purpose="waitlist"), store.connection() as connection:
        assert (
            connection.execute(
                "UPDATE product_records SET payload = '{}' WHERE collection = 'plugins'"
            ).rowcount
            == 0
        )


def test_migration_24_is_forward_only_and_idempotent(
    isolated_postgres_url, monkeypatch
):
    assert SCHEMA_HEAD_VERSION >= 24 and WAITLIST_MIGRATION[0] == 24
    previous = tuple(m for m in store_module._SCHEMA_MIGRATIONS if m[0] < 24)
    monkeypatch.setattr(store_module, "_SCHEMA_MIGRATIONS", previous)
    monkeypatch.setattr(
        store_module, "KNOWN_SCHEMA_VERSIONS", frozenset(m[0] for m in previous) | {20}
    )
    ProductPostgresStore(isolated_postgres_url).close()
    monkeypatch.undo()
    names = {
        "waitlist_entry_email_uq",
        "waitlist_submission_token_uq",
        "waitlist_submission_entry_idx",
        "waitlist_submission_created_idx",
    }
    with psycopg.connect(isolated_postgres_url, autocommit=True) as connection:
        assert not names & {
            r[0] for r in connection.execute("SELECT indexname FROM pg_indexes")
        }
        ProductPostgresStore(isolated_postgres_url).close()
        ProductPostgresStore(isolated_postgres_url).close()
        for statement in WAITLIST_MIGRATION[1]:
            connection.execute(statement)  # re-running by hand changes nothing
        found = {r[0] for r in connection.execute("SELECT indexname FROM pg_indexes")}
        versions = [
            r[0]
            for r in connection.execute(
                "SELECT version FROM product_schema_migrations ORDER BY version"
            )
        ]
    assert names <= found and [v for v in versions if v >= 23][:2] == [23, 24]


def _operator(stack, capsys, *args):
    status, out, logs = run_script(waitlist, capsys, stack["owner"], *args)
    return status, out, logs


def test_operator_script_reads_exports_changes_and_deletes_on_postgres(stack, capsys):
    client = client_for(stack["api"])
    token = join(client, EMAIL).json()["token"]
    answer(client, token, "company_location", "europe")
    join(client_for(stack["api"], "198.51.100.3"), "bob@gmail.com")
    op = ("--operator", "founder@weaves.test")
    status, out, _ = _operator(stack, capsys, "list")
    rows = json.loads(out)
    assert status == 0 and len(rows) == 2 and token not in out
    assert rows[0]["answers"] == {"company_location": "Europe (EU/EEA)"}
    _, out, _ = _operator(stack, capsys, "export", "--csv", *op)
    exported = list(csv.DictReader(io.StringIO(out)))
    assert exported[0]["company_location"] == "Europe (EU/EEA)" and token not in out
    assert json.loads(_operator(stack, capsys, "stats")[1])["entries"] == 2
    _operator(
        stack, capsys, "set-status", "--email", EMAIL, "--status", "reviewed", *op
    )
    stored = json.loads(waitlist_rows(stack)[0][2])
    assert (
        stored["status"] == "reviewed"
        and stored["history"][0]["actor_kind"] == "operator"
    )
    status, out, _ = _operator(stack, capsys, "delete", "--email", EMAIL, *op)
    assert status == 0 and json.loads(out)["submissions_deleted"] == 1
    remaining = waitlist_rows(stack)
    assert len(remaining) == 2 and all(
        "bob@gmail.com" in r[2] or r[0] == "waitlist_submissions" for r in remaining
    )
    assert (
        client.get(
            f"{URL}/submissions/current", headers=token_headers(token)
        ).status_code
        == 401
    )


def test_retention_purges_through_the_restricted_role(stack):
    client = client_for(stack["api"])
    join(client, "old@acme-corp.test")
    join(client, "fresh@acme-corp.test")
    (old_id,) = sql(
        stack,
        "SELECT record_id FROM product_records WHERE collection = 'waitlist_entries'"
        " AND payload->>'email' = 'old@acme-corp.test'",
    )[0]
    sql(
        stack,
        "UPDATE product_records SET payload = jsonb_set(payload, '{updated_at}', "
        "to_jsonb('2020-01-01T00:00:00Z'::text)) "
        "WHERE collection = 'waitlist_submissions' AND payload->>'entry_id' = %s",
        old_id,
    )
    assert stack["api"].waitlist_service.purge_stale(days=365) == (1, 1)
    left = waitlist_rows(stack)
    assert len(left) == 2 and "fresh@acme-corp.test" in left[0][2]


def test_a_long_window_budget_survives_pruning_by_its_sibling(stack):
    from datetime import timedelta

    from infrastructure.auth.throttle import PostgresLoginRateLimiter
    from shared.clock import utc_now

    pool = store_of(stack).pool
    base = PostgresLoginRateLimiter(pool, limit=5, window_seconds=900, prune_every=1)
    hourly = base.sibling(limit=2, window_seconds=3600)
    start = utc_now()
    assert hourly.consume("budget", start).allowed
    assert hourly.consume("budget", start).allowed
    base.consume("other", start + timedelta(minutes=31))  # prunes every call
    blocked = hourly.consume("budget", start + timedelta(minutes=32))
    assert not blocked.allowed and blocked.retry_after_seconds > 0
