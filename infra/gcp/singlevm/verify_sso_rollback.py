"""Rehearse SSO rollback using the baseline image and a disposable test database."""

from __future__ import annotations

import argparse
import json
import os
import runpy
from pathlib import Path

import httpx
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.store import ProductPostgresStore


def counts(store: ProductPostgresStore) -> dict[str, int]:
    """Read collection counts without exposing record bodies or credentials."""
    with store.connection() as connection:
        return dict(
            connection.execute(
                "SELECT collection, count(*) FROM product_records GROUP BY collection"
            ).fetchall()
        )


def write_future_records(store, sources, session, principal) -> str:
    """Use the actual future contracts rather than a hand-written wire fixture."""
    future_auth = runpy.run_path(str(sources / "auth.py"))
    future_sso = runpy.run_path(str(sources / "sso.py"))
    payload = json.loads(session.model_dump_json())
    payload.update(id="rehearsal-browser-session", channel="browser")
    browser = future_auth["UserSession"].model_validate_json(json.dumps(payload))
    browser_payload = json.loads(browser.model_dump_json())
    assert browser_payload["channel"] == "browser"
    connection = future_sso["SsoConnection"](
        id="rehearsal-connection",
        org_id=principal.org_id,
        provider=future_sso["SsoProvider"].GOOGLE,
        status=future_sso["SsoConnectionStatus"].ENABLED,
        issuer="https://accounts.google.com",
        client_id="rehearsal-client",
        client_secret_ref="fixture-only-secret-reference",
        created_at=session.created_at,
        updated_at=session.updated_at,
    )
    identity = future_sso["SsoExternalIdentity"](
        id="rehearsal-identity",
        org_id=principal.org_id,
        connection_id=connection.id,
        issuer=connection.issuer,
        subject="fictional-provider-subject",
        user_id=principal.user_id,
        created_by_principal_id=principal.id,
        created_at=session.created_at,
    )
    with store.connection() as conn:
        # Keep a distinct token digest without printing or persisting a plaintext token.
        import hashlib

        token = "fictional-browser-rollback-token"
        browser_payload["token_digest"] = hashlib.sha256(token.encode()).hexdigest()
        conn.execute(
            "INSERT INTO product_records(collection, record_id, org_id, payload) "
            "VALUES ('user_sessions', %s, %s, %s)",
            (browser.id, principal.org_id, Jsonb(browser_payload)),
        )
        # Also cover the first PR's explicit bearer field, even though the fix
        # emits the older compact bearer shape for forward/backward compatibility.
        conn.execute(
            "UPDATE product_records SET payload = payload || %s::jsonb "
            "WHERE collection = 'user_sessions' AND record_id = %s",
            (Jsonb({"channel": "bearer"}), session.id),
        )
        for collection, record in (
            ("sso_connections", connection),
            ("sso_external_identities", identity),
        ):
            conn.execute(
                "INSERT INTO product_records(collection, record_id, org_id, payload) "
                "VALUES (%s, %s, %s, %s)",
                (
                    collection,
                    record.id,
                    principal.org_id,
                    Jsonb(record.model_dump(mode="json")),
                ),
            )
    return token


def rehearse(sources: Path, database_url: str) -> dict[str, object]:
    """Apply the exact future migration, then restart the deployed baseline reader."""
    if conninfo_to_dict(database_url).get("dbname") != "weaves_rollback_test":
        raise ValueError("Only the disposable weaves_rollback_test database is allowed")
    baseline = ProductPostgresStore(database_url)
    runtime = compose_platform_runtime(http_client=httpx.Client(), store=baseline)
    runtime.bootstrap()
    principal = runtime.principals.get("local-developer")
    session, bearer_token = runtime.authentication_service.create_user_session(
        principal
    )
    assert "channel" not in json.loads(session.model_dump_json())
    with baseline.connection() as connection:
        assert (
            connection.execute(
                "SELECT max(version) FROM product_schema_migrations"
            ).fetchone()[0]
            == 18
        )
    runtime.close()

    future_type = runpy.run_path(str(sources / "store.py"))["ProductPostgresStore"]
    future = future_type(database_url)
    browser_token = write_future_records(future, sources, session, principal)
    before = counts(future)
    future.close()
    rollback_store = ProductPostgresStore(database_url)
    rollback = compose_platform_runtime(
        http_client=httpx.Client(), store=rollback_store
    )
    try:
        rollback.bootstrap()
        assert counts(rollback_store) == before
        auth = rollback.authentication_service
        assert auth.authenticate_user_session(bearer_token) == principal.id
        assert auth.authenticate_user_session(browser_token) is None
        assert (
            rollback.user_sessions.get("rehearsal-browser-session").channel == "browser"
        )
        auth.revoke_user_session(bearer_token, requested_by_principal_id=principal.id)
        auth.revoke_user_session(browser_token, requested_by_principal_id=principal.id)
        assert auth.authenticate_user_session(bearer_token) is None
        assert (
            rollback.user_sessions.get("rehearsal-browser-session").channel == "browser"
        )
        with rollback_store.connection() as connection:
            assert (
                connection.execute(
                    "SELECT max(version) FROM product_schema_migrations"
                ).fetchone()[0]
                == 19
            )
        after = counts(rollback_store)
        assert all(
            after.get(key) == value
            for key, value in before.items()
            if key != "audit_events"
        )
        return {
            "passed": True,
            "schema_version": 19,
            "sessions_preserved": after["user_sessions"],
            "identities_preserved": after["sso_external_identities"],
            "bearer_login_and_revocation": True,
            "browser_rejected_as_bearer": True,
            "non_audit_row_counts_preserved": True,
        }
    finally:
        rollback.close()


def main() -> None:
    """Require an explicit disposable DSN; never default to production DATABASE_URL."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--future-source", required=True, type=Path)
    args = parser.parse_args()
    database_url = os.environ["WEAVES_ROLLBACK_TEST_DATABASE_URL"]
    print(json.dumps(rehearse(args.future_source, database_url), sort_keys=True))


if __name__ == "__main__":
    main()
