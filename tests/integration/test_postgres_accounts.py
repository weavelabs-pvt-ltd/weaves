"""Accounts, identities and memberships are unique and isolated in PostgreSQL."""

import psycopg.errors
import pytest

from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    AccountStatus,
    MembershipStatus,
    OrganizationMembership,
)
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from shared.clock import utc_now
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)


def _account(account_id):
    now = utc_now()
    return Account(
        id=account_id,
        display_name="Person",
        status=AccountStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )


def _identity(identity_id, account_id, subject="person@example.test", verified=True):
    now = utc_now()
    return AccountIdentity(
        id=identity_id,
        account_id=account_id,
        kind=AccountIdentityKind.EMAIL,
        subject=subject,
        verified_at=now if verified else None,
        created_at=now,
        updated_at=now,
    )


def _membership(membership_id, account_id, org_id, user_id):
    now = utc_now()
    return OrganizationMembership(
        id=membership_id,
        org_id=org_id,
        account_id=account_id,
        user_id=user_id,
        principal_id=f"principal-{user_id}",
        status=MembershipStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def stores(isolated_postgres_url):
    owner = ProductPostgresStore(isolated_postgres_url)
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=1)
    restricted = ProductPostgresStore(pool=pool, app_role="weaves_app")
    yield owner, restricted
    restricted.close()
    owner.close()
    require_tenant_context(False)


def test_a_verified_identity_belongs_to_one_account_and_memberships_are_unique(stores):
    owner, _ = stores
    repos = create_product_repository_set(owner)
    repos.get("accounts").create(_account("a1"))
    repos.get("accounts").create(_account("a2"))
    identities = repos.get("account_identities")
    identities.create(_identity("i1", "a1"))

    with pytest.raises(ValueError) as taken:  # same verified email, other account
        identities.create(_identity("i2", "a2"))
    assert isinstance(taken.value.__cause__, psycopg.errors.UniqueViolation)
    identities.create(_identity("i3", "a2", verified=False))  # unverified claim is fine
    identities.create(_identity("i4", "a2", subject="other@example.test"))

    memberships = repos.get("memberships")
    memberships.create(_membership("m1", "a1", "org-a", "user-1"))
    for duplicate in (
        _membership("m2", "a1", "org-a", "user-2"),  # account twice in one org
        _membership("m3", "a2", "org-a", "user-1"),  # tenant user linked twice
    ):
        with pytest.raises(ValueError) as failure:
            memberships.create(duplicate)
        assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)
    memberships.create(_membership("m4", "a1", "org-b", "user-9"))  # other org is fine


def test_account_records_are_invisible_to_tenants_and_visible_to_account_scope(stores):
    owner, restricted = stores
    repos = create_product_repository_set(owner)
    repos.get("accounts").create(_account("a1"))
    repos.get("account_identities").create(_identity("i1", "a1"))
    repos.get("memberships").create(_membership("m1", "a1", "org-a", "user-1"))
    repos.get("memberships").create(_membership("m2", "a1", "org-b", "user-2"))

    def visible(*collections):
        with restricted.connection() as connection:
            rows = connection.execute(
                "SELECT collection, record_id FROM product_records "
                "WHERE collection = ANY(%s) ORDER BY collection, record_id",
                (list(collections),),
            ).fetchall()
        return [tuple(row) for row in rows]

    token = set_request_identity("principal", "org-a", "ws")
    try:  # a tenant sees only its own membership and never accounts or identities
        assert visible("accounts", "account_identities", "memberships") == [
            ("memberships", "m1")
        ]
    finally:
        reset_request_principal(token)

    require_tenant_context(True)
    assert visible("accounts", "account_identities", "memberships") == []
    with system_scope("account"):
        assert visible("accounts", "account_identities", "memberships") == [
            ("account_identities", "i1"),
            ("accounts", "a1"),
            ("memberships", "m1"),
            ("memberships", "m2"),
        ]
    with system_scope("queue"):
        assert visible("accounts", "account_identities", "memberships") == []
