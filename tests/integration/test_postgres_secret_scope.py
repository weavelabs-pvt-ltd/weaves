"""Stored secrets belong to one organization and cannot be read across tenants."""

import pytest
from cryptography.fernet import Fernet

from infrastructure.database.store import ProductPostgresStore
from infrastructure.secrets.store import EncryptedPostgresSecretStore
from shared.request_context import reset_request_principal, set_request_identity


def _as(org_id, workspace_id="ws"):
    return set_request_identity("principal", org_id, workspace_id)


def test_secrets_are_owned_by_one_organization(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        secrets = EncryptedPostgresSecretStore(
            store.pool, Fernet.generate_key().decode()
        )
        token = _as("org-a")
        ref = secrets.put("a-secret", purpose="model_provider", owner_ref="provider:1")
        assert secrets.get(ref) == "a-secret"
        reset_request_principal(token)

        token = _as("org-b")
        with pytest.raises(KeyError):
            secrets.get(ref)
        with pytest.raises(KeyError):
            secrets.replace(ref, "stolen")
        secrets.delete(ref)  # silently ignored: not b's secret
        reset_request_principal(token)

        token = _as("org-a")
        assert secrets.get(ref) == "a-secret"
        with store.connection() as connection:
            row = connection.execute(
                "SELECT org_id, workspace_id, purpose, owner_ref, key_id "
                "FROM product_secrets WHERE secret_ref = %s",
                (ref,),
            ).fetchone()
        assert row[:4] == ("org-a", "ws", "model_provider", "provider:1")
        assert row[4]
        secrets.replace(ref, "rotated")
        assert secrets.get(ref) == "rotated"
        secrets.delete(ref)
        with pytest.raises(KeyError):
            secrets.get(ref)
        reset_request_principal(token)
    finally:
        store.close()


def test_legacy_unowned_secrets_are_backfilled_from_unique_references_only(
    isolated_postgres_url,
):
    store = ProductPostgresStore(isolated_postgres_url)
    key = Fernet.generate_key().decode()
    try:
        cipher = Fernet(key.encode())
        EncryptedPostgresSecretStore(store.pool, key)  # create current table shape
        with store.connection() as connection:
            for ref in ("secret:owned", "secret:shared", "secret:orphan"):
                connection.execute(
                    "INSERT INTO product_secrets (secret_ref, ciphertext) "
                    "VALUES (%s, %s)",
                    (ref, cipher.encrypt(ref.encode())),
                )
            for record_id, org_id, ref in (
                ("provider-a", "org-a", "secret:owned"),
                ("provider-b1", "org-a", "secret:shared"),
                ("provider-b2", "org-b", "secret:shared"),
            ):
                connection.execute(
                    "INSERT INTO product_records (collection, record_id, org_id, "
                    "workspace_id, payload) VALUES ('model_providers', %s, %s, "
                    "NULL, %s::jsonb)",
                    (record_id, org_id, f'{{"auth_ref": "{ref}"}}'),
                )
        secrets = EncryptedPostgresSecretStore(store.pool, key)  # backfills

        token = _as("org-a")
        assert secrets.get("secret:owned") == "secret:owned"
        for unowned in ("secret:shared", "secret:orphan"):
            with pytest.raises(KeyError):
                secrets.get(unowned)
        reset_request_principal(token)
        token = _as("org-b")
        with pytest.raises(KeyError):
            secrets.get("secret:owned")
        reset_request_principal(token)
    finally:
        store.close()
