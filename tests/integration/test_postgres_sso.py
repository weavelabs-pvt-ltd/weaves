"""Database-enforced SSO subject uniqueness and immutability."""

from datetime import UTC, datetime

import psycopg.errors
import pytest

from contracts.v1 import SsoExternalIdentity
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)


def test_postgres_sso_bindings_reject_duplicates_and_rewrites(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        identities = create_product_repository_set(store).get("sso_external_identities")
        original = SsoExternalIdentity(
            id="identity-a",
            org_id="org-a",
            connection_id="connection-a",
            issuer="https://accounts.google.com",
            subject="subject-a",
            user_id="user-a",
            created_by_principal_id="admin-a",
            created_at=datetime.now(UTC),
        )
        identities.create(original)
        for updates in (
            {"id": "duplicate-subject", "user_id": "different-user"},
            {"id": "duplicate-user", "subject": "different-subject"},
        ):
            with pytest.raises(ValueError) as failure:
                identities.create(original.model_copy(update=updates))
            assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)

        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            identities.put(original.model_copy(update={"subject": "rewritten"}))
        assert identities.get(original.id) == original

        # The same provider identity may belong to independently approved tenants.
        identities.create(
            original.model_copy(
                update={
                    "id": "identity-b",
                    "org_id": "org-b",
                    "connection_id": "connection-b",
                    "user_id": "user-b",
                    "created_by_principal_id": "admin-b",
                }
            )
        )
        assert len(identities.list_scoped("org-a")) == 1
        with pytest.raises(KeyError):
            identities.get_scoped("identity-a", "org-b")
    finally:
        store.close()
