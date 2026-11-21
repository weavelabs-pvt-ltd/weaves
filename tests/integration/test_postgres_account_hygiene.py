"""Indexed lookups, bounded purge and link audit under the restricted role."""

from datetime import timedelta

import pytest

from contracts.v1 import AccountIdentity, AccountIdentityKind
from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from modules.auth.account_cleanup import PURGE_GRACE
from modules.auth.account_sign_in import AccountSignInService
from shared.clock import new_id, utc_now
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.account_rows import challenge_row, session_row
from tests.support.account_world import (
    HMAC_KEY,
    CapturedMail,
    seed_member,
    seed_organization,
)

LONG_AGO = utc_now() - PURGE_GRACE - timedelta(minutes=5)


@pytest.fixture
def stack(isolated_postgres_url):
    owner = ProductPostgresStore(isolated_postgres_url)
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=1)
    restricted = ProductPostgresStore(pool=pool, app_role="weaves_app")
    seed = create_product_repository_set(owner)
    for org in ("org-a", "org-b"):
        seed_organization(seed.get("organizations"), org)
    seed_member(seed.get("users"), seed.get("principals"), "org-a", "pat@example.test")
    seed_member(seed.get("users"), seed.get("principals"), "org-b", "bo@example.test")
    repos = create_product_repository_set(restricted)
    mail = CapturedMail()
    service = AccountSignInService(
        accounts=repos.get("accounts"),
        identities=repos.get("account_identities"),
        challenges=repos.get("account_login_challenges"),
        sessions=repos.get("account_sessions"),
        memberships=repos.get("memberships"),
        users=repos.get("users"),
        principals=repos.get("principals"),
        organizations=repos.get("organizations"),
        workspaces=repos.get("workspaces"),
        audit_events=repos.get("audit_events"),
        email_sender=mail,
        email_login_hmac_key=HMAC_KEY,
        login_rate_limiter=InMemoryLoginRateLimiter(limit=50),
        unit_of_work=restricted.transaction,
    )
    require_tenant_context(True)
    yield service, mail, repos, owner
    require_tenant_context(False)
    restricted.close()
    owner.close()


def _sign_in(service, mail, email):
    challenge = service.start_email_login(email, "198.51.100.7")
    return service.verify_email_login(challenge, mail.code())


def _link_events(repos, org_id):
    token = set_request_identity("reader", org_id, "ws")
    try:
        return [
            e
            for e in repos.get("audit_events").list_scoped(org_id)
            if e.action == "account.membership.linked"
        ]
    finally:
        reset_request_principal(token)


def _identity(account_id, subject):
    now = utc_now()
    return AccountIdentity(
        id=new_id(),
        account_id=account_id,
        kind=AccountIdentityKind.EMAIL,
        subject=subject,
        verified_at=now,
        created_at=now,
        updated_at=now,
    )


def test_list_by_field_under_the_restricted_role(stack):
    service, _, _, _owner = stack
    rows = [
        _identity("acct-1", "b@example.test"),
        _identity("acct-1", "a@example.test"),
        _identity("acct-2", "c@example.test"),
    ]
    with system_scope("account"):
        for row in rows:
            service.identities.create(row)
        found = service.identities.list_by_field("account_id", "acct-1")
        assert [i.id for i in found] == sorted(i.id for i in found)
        assert {i.subject for i in found} == {"a@example.test", "b@example.test"}
        assert service.identities.list_by_field("account_id", "none") == ()
        assert (
            len(
                service.identities.list_by_field(
                    "subject", "c@EXAMPLE.test", casefold=True
                )
            )
            == 1
        )
        assert service.identities.list_by_field("subject", "c@EXAMPLE.test") == ()
        with pytest.raises(ValueError):
            service.identities.list_by_field("account_id; DROP", "x")
    assert service.identities.list_by_field("account_id", "acct-1") == ()  # RLS


def test_purge_under_the_restricted_role_is_bounded_and_idempotent(stack):
    service = stack[0]
    now = utc_now()
    live_session = session_row("a", now + timedelta(hours=1))
    with system_scope("account"):
        for i in range(4):
            service.challenges.create(challenge_row(f"x{i}@example.test", LONG_AGO))
        service.challenges.create(
            challenge_row("live@example.test", now + timedelta(minutes=5))
        )
        service.sessions._sessions.create(session_row("a", LONG_AGO))
        service.sessions._sessions.create(
            session_row("a", now + timedelta(hours=1), revoked_at=LONG_AGO)
        )
        service.sessions._sessions.create(live_session)
    assert service.purge_expired(limit=3) == 3
    assert service.purge_expired(limit=3) == 3
    assert service.purge_expired(limit=3) == 0
    with system_scope("account"):
        assert [c.email for c in service.challenges.list()] == ["live@example.test"]
        assert [s.id for s in service.sessions._sessions.list()] == [live_session.id]


def test_link_audit_is_written_for_the_right_organization_only(stack):
    service, mail, repos, _owner = stack
    with system_scope("account"):
        account, _, _ = _sign_in(service, mail, "pat@example.test")
    (event,) = _link_events(repos, "org-a")
    assert event.org_id == "org-a" and event.metadata == {"account_id": account.id}
    assert "pat@example.test" not in event.model_dump_json()
    assert _link_events(repos, "org-b") == []


def test_audit_failure_rolls_the_membership_back(stack):
    service, mail, repos, _owner = stack
    audit = service._linker._audit_events
    real_create = audit.create

    def broken(event):
        raise RuntimeError("audit store down")

    audit.create = broken
    with system_scope("account"):
        with pytest.raises(RuntimeError):
            _sign_in(service, mail, "pat@example.test")
        assert service.memberships.list() == ()
        audit.create = real_create
        _sign_in(service, mail, "pat@example.test")
        assert len(service.memberships.list()) == 1
    assert len(_link_events(repos, "org-a")) == 1


@pytest.mark.parametrize(
    ("collection", "field", "value"),
    [
        ("account_identities", "account_id", "acct-1"),
        ("account_identities", "subject", "a@example.test"),
        ("memberships", "account_id", "acct-1"),
        ("memberships", "user_id", "user-1"),
        ("account_sessions", "account_id", "acct-1"),
        ("account_login_challenges", "email", "a@example.test"),
    ],
)
def test_each_lookup_is_served_by_an_index(stack, collection, field, value):
    _, _, _, owner = stack
    with owner.connection() as connection:
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                f"""EXPLAIN SELECT payload FROM product_records
                    WHERE collection = '{collection}'
                      AND payload ->> '{field}' = '{value}' ORDER BY record_id""",
            ).fetchall()
        )
    assert "Seq Scan" not in plan, plan


@pytest.mark.parametrize(
    ("collection", "field"),
    [
        ("account_login_challenges", "expires_at"),
        ("account_sessions", "expires_at"),
        ("account_sessions", "revoked_at"),
    ],
)
def test_purge_queries_are_served_by_an_index(stack, collection, field):
    _, _, _, owner = stack
    with owner.connection() as connection:
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                f"""EXPLAIN SELECT payload FROM product_records
                    WHERE collection = '{collection}'
                      AND payload ->> '{field}' < '2030-01-01T00:00:00Z'
                    ORDER BY payload ->> '{field}', record_id LIMIT 10""",
            ).fetchall()
        )
    assert "Seq Scan" not in plan, plan
    assert "Sort" not in plan, plan


def test_user_email_lookup_is_served_by_an_index(stack):
    _, _, _, owner = stack
    with owner.connection() as connection:
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                """EXPLAIN SELECT payload FROM product_records
                   WHERE collection = 'users'
                     AND lower(payload ->> 'email') = lower('a@example.test')
                   ORDER BY record_id"""
            ).fetchall()
        )
    assert "Seq Scan" not in plan, plan
