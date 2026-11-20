"""Account sign-in works under the restricted database role and stays private."""

import psycopg
import pytest

from contracts.v1 import AccountSession, MembershipStatus
from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from modules.auth.account_sign_in import AccountSignInService
from shared.clock import new_id, utc_now
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.account_world import (
    HMAC_KEY,
    CapturedMail,
    seed_member,
    seed_organization,
)


@pytest.fixture
def stack(isolated_postgres_url):
    owner = ProductPostgresStore(isolated_postgres_url)
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=1)
    restricted = ProductPostgresStore(pool=pool, app_role="weaves_app")
    seed = create_product_repository_set(owner)
    for org in ("org-a", "org-b"):
        seed_organization(seed.get("organizations"), org)
        seed_member(seed.get("users"), seed.get("principals"), org, "pat@example.test")
    seed_member(
        seed.get("users"), seed.get("principals"), "org-b", "other@example.test"
    )
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
        email_sender=mail,
        email_login_hmac_key=HMAC_KEY,
        login_rate_limiter=InMemoryLoginRateLimiter(limit=50),
        unit_of_work=restricted.transaction,
    )
    require_tenant_context(True)  # production posture: no implicit local tenant
    yield service, mail, restricted, owner
    require_tenant_context(False)
    restricted.close()
    owner.close()


def _count(store, collection):
    with store.connection() as connection:
        return connection.execute(
            "SELECT count(*) FROM product_records WHERE collection = %s", (collection,)
        ).fetchone()[0]


def _sign_in(service, mail, email):
    challenge = service.start_email_login(email, "198.51.100.7")
    return service.verify_email_login(challenge, mail.code())


def test_sign_in_links_memberships_under_the_restricted_role(stack):
    service, mail, restricted, _ = stack
    with system_scope("account"):
        account, session, token = _sign_in(service, mail, "Pat@Example.test")
        again, _, _ = _sign_in(service, mail, "pat@example.test")
        assert again.id == account.id
        assert service.sessions.authenticate(token).id == account.id
        memberships = [m for m in service.memberships.list()]
        assert {m.org_id for m in memberships} == {"org-a", "org-b"}
        assert {m.account_id for m in memberships} == {account.id}
        assert all(m.status is MembershipStatus.ACTIVE for m in memberships)
        service.sessions.revoke(token)
        with pytest.raises(PermissionError):
            service.sessions.authenticate(token)
        assert _count(restricted, "accounts") == 1
        assert _count(restricted, "account_identities") == 1
        assert _count(restricted, "account_login_challenges") == 0
        stored = service.sessions._sessions.get(session.id)
        assert token not in stored.model_dump_json()


def test_account_rows_are_invisible_to_tenants_and_other_system_scopes(stack):
    service, mail, restricted, _ = stack
    with system_scope("account"):
        _sign_in(service, mail, "pat@example.test")
        service.start_email_login("pending@example.test", "198.51.100.8")
    collections = (
        "accounts",
        "account_identities",
        "account_login_challenges",
        "account_sessions",
    )
    token = set_request_identity("principal", "org-a", "ws")
    try:
        assert [_count(restricted, name) for name in collections] == [0, 0, 0, 0]
    finally:
        reset_request_principal(token)
    assert [_count(restricted, name) for name in collections] == [0, 0, 0, 0]
    with system_scope("queue"):
        assert [_count(restricted, name) for name in collections] == [0, 0, 0, 0]
    with system_scope("auth"):
        assert [_count(restricted, name) for name in collections] == [1, 1, 1, 1]
    with system_scope("account"):
        assert [_count(restricted, name) for name in collections] == [1, 1, 1, 1]


def test_without_the_account_scope_the_database_refuses_account_writes(stack):
    service, _, _, _ = stack
    with pytest.raises(psycopg.Error):
        service.start_email_login("blocked@example.test", "198.51.100.9")


def test_a_lost_identity_race_reuses_the_winner_and_leaves_no_orphan(stack):
    service, mail, restricted, _ = stack
    with system_scope("account"):
        winner, _, _ = _sign_in(service, mail, "racer@example.test")
        real_lookup = service._verified_identity
        calls = []

        def stale_lookup(email):
            calls.append(email)
            return None if len(calls) == 1 else real_lookup(email)

        service._verified_identity = stale_lookup
        challenge = service.start_email_login("racer@example.test", "198.51.100.7")
        loser, _, _ = service.verify_email_login(challenge, mail.code())
        assert loser.id == winner.id
        assert _count(restricted, "accounts") == 1
        assert _count(restricted, "account_identities") == 1


def test_relinking_is_idempotent_and_keeps_one_membership_per_org(stack):
    service, mail, restricted, _ = stack
    with system_scope("account"):
        account, _, _ = _sign_in(service, mail, "pat@example.test")
        assert service._linker.link(account, "pat@example.test") == 0
        assert _count(restricted, "memberships") == 2


def test_session_token_digests_are_unique_in_the_database(stack):
    service, mail, _, _ = stack
    with system_scope("account"):
        account, session, _ = _sign_in(service, mail, "pat@example.test")
        now = utc_now()
        duplicate = AccountSession(
            id=new_id(),
            account_id=account.id,
            token_digest=session.token_digest,
            expires_at=now,
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(ValueError) as failure:
            service.sessions._sessions.create(duplicate)
        assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)


def test_browser_channel_sessions_are_isolated_under_the_restricted_role(stack):
    service, mail, restricted, _ = stack
    with system_scope("account"):
        challenge = service.start_email_login("pat@example.test", "198.51.100.7")
        account, session, cookie = service.verify_email_login(
            challenge, mail.code(), channel="browser"
        )
        _, bearer_session, bearer = _sign_in(service, mail, "pat@example.test")
        assert session.channel == "browser" and bearer_session.channel == "bearer"
        assert cookie.startswith("wvs_acbr_") and bearer.startswith("wvs_acct_")
        assert service.sessions.authenticate(cookie, "browser").id == account.id
        for token, channel in ((cookie, "bearer"), (bearer, "browser")):
            with pytest.raises(PermissionError):
                service.sessions.authenticate(token, channel)
        service.sessions.revoke(cookie, "browser")
        with pytest.raises(PermissionError):
            service.sessions.authenticate(cookie, "browser")
        assert service.sessions.authenticate(bearer).id == account.id
        assert _count(restricted, "account_sessions") == 2
        stored = service.sessions._sessions.get(session.id)
        assert stored.channel == "browser" and cookie not in stored.model_dump_json()
