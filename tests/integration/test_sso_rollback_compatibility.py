"""The compatibility release can run against future SSO data without a reset."""

import hashlib

from contracts.v1 import UserSession
from infrastructure.database.store import ProductPostgresStore
from tests.support.runtime import LocalPlatformRuntime


def test_additive_sso_schema_and_new_sessions_survive_application_rollback(
    isolated_postgres_url,
):
    store = ProductPostgresStore(isolated_postgres_url)
    runtime = LocalPlatformRuntime(postgres_store=store)
    runtime.bootstrap()
    principal = runtime.principals.get("local-developer")
    session, token = runtime.authentication_service.create_user_session(principal)
    browser_token = "fictional-browser-session-for-rollback"
    browser = session.model_copy(
        update={
            "id": "rollback-browser-session",
            "channel": "browser",
            "token_digest": hashlib.sha256(browser_token.encode()).hexdigest(),
        }
    )
    runtime.user_sessions.create(browser)
    # Simulate the new writer's explicit bearer channel as well as the compact
    # legacy shape. Both must remain readable by the rollback application.
    with store.connection() as connection:
        connection.execute(
            "UPDATE product_records SET payload = payload || %s::jsonb "
            "WHERE collection = 'user_sessions' AND record_id = %s",
            ('{"channel":"bearer"}', session.id),
        )
        connection.execute(
            "INSERT INTO product_schema_migrations(version) VALUES (19) "
            "ON CONFLICT DO NOTHING"
        )
    runtime.close()

    reopened = ProductPostgresStore(isolated_postgres_url)
    rollback = LocalPlatformRuntime(postgres_store=reopened)
    try:
        rollback.bootstrap()
        assert (
            rollback.authentication_service.authenticate_user_session(token)
            == principal.id
        )
        assert (
            rollback.authentication_service.authenticate_user_session(browser_token)
            is None
        )
        persisted = rollback.user_sessions.get(browser.id)
        assert (
            UserSession.model_validate_json(persisted.model_dump_json()).channel
            == "browser"
        )
        rollback.authentication_service.revoke_user_session(
            token, requested_by_principal_id=principal.id
        )
        assert rollback.authentication_service.authenticate_user_session(token) is None
        with reopened.connection() as connection:
            assert (
                connection.execute(
                    "SELECT max(version) FROM product_schema_migrations"
                ).fetchone()[0]
                == 21
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM product_records "
                    "WHERE collection = 'user_sessions'"
                ).fetchone()[0]
                == 2
            )
    finally:
        rollback.close()
