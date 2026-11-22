"""Migration 22 removes legacy sign-in rows and objects and nothing else."""

from __future__ import annotations

import psycopg
import pytest

from infrastructure.database import store as store_module
from infrastructure.database.legacy_purge import (
    DROPPED_INDEXES,
    LEGACY_PURGE_MIGRATION,
    PURGED_COLLECTIONS,
)
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from infrastructure.persistence.product_repositories import REPOSITORY_SPECS
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
)

REGISTRY = {spec[1] for spec in REPOSITORY_SPECS}
PREVIOUS_HEAD = LEGACY_PURGE_MIGRATION[0] - 1


@pytest.fixture
def release_21(monkeypatch):
    """Make startup stop at migration 21, the last release before the purge."""
    old = tuple(m for m in store_module._SCHEMA_MIGRATIONS if m[0] <= PREVIOUS_HEAD)
    monkeypatch.setattr(store_module, "_SCHEMA_MIGRATIONS", old)
    monkeypatch.setattr(
        store_module, "KNOWN_SCHEMA_VERSIONS", frozenset(m[0] for m in old) | {20}
    )


def _versions(url):
    with psycopg.connect(url) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT version FROM product_schema_migrations ORDER BY version"
            )
        ]


def _scalar(url, sql, *params):
    with psycopg.connect(url) as connection:
        return connection.execute(sql, params).fetchall()


def _seed(url):
    """Legacy rows in every purged collection plus kept rows, all with org ids."""
    with psycopg.connect(url) as connection:
        for index, collection in enumerate(PURGED_COLLECTIONS):
            for org in ("org-a", "org-b"):
                connection.execute(
                    "INSERT INTO product_records "
                    "(collection, record_id, org_id, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (
                        collection,
                        f"{collection}-{org}",
                        org,
                        f'{{"token_digest": "d{index}{org}", "connection_id": "c",'
                        f' "issuer": "i", "subject": "s{index}{org}",'
                        f' "user_id": "u{index}{org}", "provider": "p"}}',
                    ),
                )
        for org in ("org-a", "org-b"):
            connection.execute(
                "INSERT INTO product_records "
                "(collection, record_id, org_id, payload) VALUES "
                "('organizations', %s, NULL, '{\"name\": \"é ünï\"}'), "
                "('users', %s, %s, %s::jsonb), "
                "('api_credentials', %s, %s, %s::jsonb), "
                "('account_sessions', %s, NULL, %s::jsonb)",
                (
                    org,
                    f"user-{org}",
                    org,
                    f'{{"email": "Person@{org}.test", "status": "active"}}',
                    f"cred-{org}",
                    org,
                    f'{{"token_digest": "api-{org}"}}',
                    f"sess-{org}",
                    f'{{"token_digest": "acct-{org}"}}',
                ),
            )
        connection.execute(
            "INSERT INTO product_login_throttle VALUES (%s, now(), 1, now())",
            ("bucket",),
        )


KEPT_SQL = (
    "SELECT collection, record_id, org_id, workspace_id, idempotency_key, "
    "requested_by_principal_id, payload::text, created_at, updated_at "
    "FROM product_records WHERE collection <> ALL(%s) "
    "ORDER BY collection, record_id"
)
LEGACY_OBJECTS_SQL = (
    "SELECT (SELECT count(*) FROM pg_indexes WHERE indexname = ANY(%s)), "
    "(SELECT count(*) FROM pg_trigger WHERE tgname = 'sso_identity_immutable'), "
    "(SELECT count(*) FROM pg_proc WHERE proname = 'reject_sso_identity_rewrite')"
)


def test_legacy_database_is_purged_and_kept_data_is_untouched(
    isolated_postgres_url, release_21, monkeypatch
):
    ProductPostgresStore(isolated_postgres_url).close()
    assert _versions(isolated_postgres_url)[-1] == PREVIOUS_HEAD
    _seed(isolated_postgres_url)
    kept = list(PURGED_COLLECTIONS)
    before = _scalar(isolated_postgres_url, KEPT_SQL, kept)
    assert before
    # Every legacy object the old migrations created is present.
    present = _scalar(isolated_postgres_url, LEGACY_OBJECTS_SQL, list(DROPPED_INDEXES))[
        0
    ]
    assert present[1:] == (1, 1)
    assert present[0] >= 3

    monkeypatch.undo()  # release the pin: this is the new release starting up
    ProductPostgresStore(isolated_postgres_url).close()

    assert _versions(isolated_postgres_url)[-1] == SCHEMA_HEAD_VERSION
    assert _scalar(isolated_postgres_url, KEPT_SQL, kept) == before
    assert not _scalar(
        isolated_postgres_url,
        "SELECT 1 FROM product_records WHERE collection = ANY(%s)",
        kept,
    )
    assert _scalar(
        isolated_postgres_url, LEGACY_OBJECTS_SQL, list(DROPPED_INDEXES)
    ) == [(0, 0, 0)]
    # The login throttle table and its rows are deliberately left alone.
    throttle = _scalar(
        isolated_postgres_url, "SELECT count(*) FROM product_login_throttle"
    )
    assert throttle == [(1,)]


def test_purge_is_idempotent_and_safe_on_empty_and_current_databases(
    isolated_postgres_url,
):
    ProductPostgresStore(isolated_postgres_url).close()  # empty database to head
    first = _versions(isolated_postgres_url)
    ProductPostgresStore(isolated_postgres_url).close()  # already at head
    assert _versions(isolated_postgres_url) == first
    with psycopg.connect(isolated_postgres_url) as connection:  # re-run by hand
        for statement in LEGACY_PURGE_MIGRATION[1]:
            connection.execute(statement)
    assert _versions(isolated_postgres_url) == first


def test_row_policies_still_isolate_tenants_after_the_purge(
    isolated_postgres_url, release_21, monkeypatch
):
    ProductPostgresStore(isolated_postgres_url).close()
    _seed(isolated_postgres_url)
    monkeypatch.undo()
    store = ProductPostgresStore(isolated_postgres_url, app_role="weaves_app")
    try:
        for org, other in (("org-a", "org-b"), ("org-b", "org-a")):
            token = set_request_identity("principal", org, "ws")
            try:
                with store.connection() as connection:
                    seen = {
                        row[0]
                        for row in connection.execute(
                            "SELECT record_id FROM product_records"
                        )
                    }
                    assert f"user-{org}" in seen
                    assert f"user-{other}" not in seen
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        connection.execute(
                            "INSERT INTO product_records (collection, record_id,"
                            " org_id, payload) VALUES ('users', 'x', %s, '{}')",
                            (other,),
                        )
            finally:
                reset_request_principal(token)
                require_tenant_context(False)
    finally:
        store.close()


# Every index of the current schema and the query or invariant that needs it.
# A new index must be added here with its reason; a removed one must leave.
INDEX_JUSTIFICATIONS = {
    "product_records_pkey": "record identity",
    "product_schema_migrations_pkey": "applied versions",
    "product_login_throttle_pkey": "one bucket per key",
    "product_login_throttle_window_idx": "bounded pruning of old buckets",
    "product_records_org_scope_idx": "tenant list and get by collection",
    "product_records_workspace_scope_idx": "workspace list by collection",
    "product_records_run_idempotency_uq": "workflow run idempotency",
    "product_records_api_token_digest_uq": "service credential lookup",
    "product_records_execution_job_idempotency_uq": "job idempotency",
    "product_records_role_binding_scope_uq": "one binding per scope",
    "product_records_execution_job_claim_idx": "worker queue claim",
    "product_records_active_user_email_uq": "one active user per email and org",
    "product_records_workflow_trigger_secret_uq": "trigger secret lookup",
    "product_records_workflow_schedule_due_idx": "scheduler due scan",
    "product_records_audit_event_org_page_idx": "audit pagination",
    "product_records_audit_event_workspace_page_idx": "audit pagination",
    "product_records_system_role_key_uq": "one system role per key and org",
    "product_records_task_idempotency_uq": "task idempotency",
    "product_records_task_turn_idempotency_uq": "turn idempotency",
    "product_records_task_page_idx": "task pagination",
    "product_records_task_messages_page_idx": "message pagination",
    "product_records_task_turn_claim_idx": "worker queue claim",
    "product_records_task_turn_stale_idx": "stale turn recovery",
    "skills_org_slug_unique_idx": "organization skill slug",
    "skills_personal_slug_unique_idx": "personal skill slug",
    "skills_version_number_unique_idx": "skill version number",
    "skills_publication_version_unique_idx": "one publication per version",
    "skills_revocation_version_unique_idx": "one revocation per version",
    "account_identity_verified_uq": "identity belongs to one account",
    "account_identity_owner_uq": "no duplicate identity per account",
    "membership_account_org_uq": "one membership per account and org",
    "membership_user_uq": "one account per tenant user",
    "membership_account_idx": "memberships of an account",
    "account_session_token_uq": "session token lookup",
    "account_identity_account_idx": "identities of an account",
    "account_identity_subject_idx": "identity lookup by subject",
    "membership_user_idx": "membership of a user",
    "account_session_account_idx": "sessions of an account",
    "account_oauth_state_uq": "Google callback state lookup",
    "account_oauth_state_expiry_idx": "purge expired sign-in states",
    "account_session_expiry_idx": "purge expired sessions",
    "account_session_revoked_idx": "purge revoked sessions",
    "account_org_request_key_uq": "organization creation idempotency and quota",
    "user_email_lower_idx": "verified-email account linking",
    "organization_lifecycle_state_idx": "worker finds pending deletions",
    "organization_slug_uq": "one organization per slug, compared without case",
    "organization_email_domain_uq": "one organization per claimed email domain",
    "waitlist_entry_email_uq": "one waitlist entry per email, without case",
    "waitlist_submission_token_uq": "resume token lookup, one submission per token",
    "waitlist_submission_entry_idx": "submissions of a waitlist entry",
    "waitlist_submission_created_idx": "waitlist sign-ups ordered by time",
}


def test_every_index_of_the_schema_is_justified(isolated_postgres_url):
    ProductPostgresStore(isolated_postgres_url).close()
    names = {
        row[0]
        for row in _scalar(
            isolated_postgres_url,
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()",
        )
    }
    assert names == set(INDEX_JUSTIFICATIONS)
    tables = {
        row[0]
        for row in _scalar(
            isolated_postgres_url,
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()",
        )
    }
    assert tables == {
        "product_records",
        "product_schema_migrations",
        "product_login_throttle",
    }
    assert not _scalar(
        isolated_postgres_url,
        "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE c.relnamespace = current_schema()::regnamespace AND NOT t.tgisinternal",
    )


def test_purged_collections_never_overlap_the_registry():
    assert not REGISTRY & set(PURGED_COLLECTIONS)
