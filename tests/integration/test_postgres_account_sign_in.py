"""Google sign-in works under the restricted database role and stays private."""

from dataclasses import dataclass

import psycopg
import pytest

from contracts.v1 import (
    AccountSession,
    MembershipStatus,
    PrincipalStatus,
    UserStatus,
)
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.google_account_resolution import SignInRefused
from modules.auth.google_oidc import CALLBACK_PATH, GoogleConfig, GoogleOidcProvider
from modules.auth.google_sign_in import GoogleSignIn
from shared.clock import new_id, utc_now
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.account_session import ORIGIN
from tests.support.account_world import seed_member, seed_organization
from tests.support.fake_google import (
    CLIENT_ID,
    CLIENT_SECRET,
    DISCOVERY,
    FakeGoogle,
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
    fake = FakeGoogle()
    service = AccountSignInService(
        accounts=repos.get("accounts"),
        identities=repos.get("account_identities"),
        oauth_states=repos.get("account_oauth_states"),
        sessions=repos.get("account_sessions"),
        memberships=repos.get("memberships"),
        users=repos.get("users"),
        principals=repos.get("principals"),
        organizations=repos.get("organizations"),
        workspaces=repos.get("workspaces"),
        audit_events=repos.get("audit_events"),
        unit_of_work=restricted.transaction,
    )
    config = GoogleConfig(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=ORIGIN + CALLBACK_PATH,
        discovery_url=DISCOVERY,
        allowed_emails=frozenset(),
        allowed_domains=frozenset(),
    )
    google = GoogleSignIn(config, GoogleOidcProvider(config, fake.client()), service)
    require_tenant_context(True)  # production posture: no implicit local tenant
    yield Stack(service, google, fake), restricted, owner
    require_tenant_context(False)
    restricted.close()
    owner.close()


def _count(store, collection):
    with store.connection() as connection:
        return connection.execute(
            "SELECT count(*) FROM product_records WHERE collection = %s", (collection,)
        ).fetchone()[0]


@dataclass
class Stack:
    """The account directory with Google sign-in wired to a fake provider."""

    service: AccountSignInService
    google: GoogleSignIn
    fake: FakeGoogle


def _sign_in(stack, email, **claims):
    """Run start, consent and callback; return (account, session, token)."""
    started = stack.google.start("/")
    query = stack.fake.authorize(started.authorization_url, email=email, **claims)
    result = stack.google.finish(
        code=query["code"],
        state=query["state"],
        binding=started.binding,
        provider_error=None,
    )
    return result.account, result.session, result.token


def test_google_sign_in_links_memberships_under_the_restricted_role(stack):
    harness, restricted, _ = stack
    service = harness.service
    with system_scope("account"):
        account, session, token = _sign_in(harness, "Pat@Example.test", sub="g-1")
        again, _, _ = _sign_in(harness, "pat@example.test", sub="g-1")
        assert again.id == account.id
        assert token.startswith("wvs_acbr_")
        assert service.sessions.authenticate(token, "browser").id == account.id
        memberships = list(service.memberships.list())
        assert {m.org_id for m in memberships} == {"org-a", "org-b"}
        assert {m.account_id for m in memberships} == {account.id}
        assert all(m.status is MembershipStatus.ACTIVE for m in memberships)
        service.sessions.revoke(token, "browser")
        with pytest.raises(PermissionError):
            service.sessions.authenticate(token, "browser")
        assert _count(restricted, "accounts") == 1
        assert _count(restricted, "account_identities") == 2  # oidc + email
        assert _count(restricted, "account_oauth_states") == 0  # consumed
        stored = service.sessions._sessions.get(session.id)
        assert token not in stored.model_dump_json()


def test_state_is_single_use_and_bound_to_its_browser_under_the_restricted_role(
    stack,
):
    harness, restricted, _ = stack
    with system_scope("account"):
        started = harness.google.start("/next")
        assert _count(restricted, "account_oauth_states") == 1
        stored = harness.service.oauth_states.list()[0]
        assert started.binding not in stored.model_dump_json()
        query = harness.fake.authorize(started.authorization_url, email="a@b.test")
        assert query["state"] not in stored.model_dump_json()
        with pytest.raises(SignInRefused) as wrong:
            harness.google.finish(
                code=query["code"],
                state=query["state"],
                binding="someone-else",
                provider_error=None,
            )
        assert wrong.value.code == "state"
        assert _count(restricted, "account_oauth_states") == 0  # burned, not reusable
        with pytest.raises(SignInRefused):
            harness.google.finish(
                code=query["code"],
                state=query["state"],
                binding=started.binding,
                provider_error=None,
            )
        assert _count(restricted, "accounts") == 0


def test_the_state_digest_is_unique_in_the_database(stack):
    harness, _, _ = stack
    with system_scope("account"):
        harness.google.start("/")
        existing = harness.service.oauth_states.list()[0]
        clone = existing.model_copy(update={"id": new_id()})
        with pytest.raises(ValueError) as failure:
            harness.service.oauth_states.create(clone)
        assert isinstance(failure.value.__cause__, psycopg.errors.UniqueViolation)


def test_sign_in_activates_invited_members_but_never_suspended_ones(stack):
    harness, _restricted, owner = stack
    service = harness.service
    seed = create_product_repository_set(owner)
    users, principals = seed.get("users"), seed.get("principals")
    for org, user_status, principal_status in (
        ("org-a", UserStatus.INVITED, PrincipalStatus.DISABLED),
        ("org-b", UserStatus.SUSPENDED, PrincipalStatus.DISABLED),
    ):
        user = next(u for u in users.list() if u.org_id == org and "pat@" in u.email)
        principal = principals.find_by_field("user_id", user.id)
        users.put(user.model_copy(update={"status": user_status}))
        principals.put(principal.model_copy(update={"status": principal_status}))
    with system_scope("account"):
        account, _, _ = _sign_in(harness, "pat@example.test")
        linked = {m.org_id for m in service.memberships.list()}
    assert linked == {"org-a"}
    by_org = {u.org_id: u for u in users.list() if "pat@" in u.email}
    assert by_org["org-a"].status is UserStatus.ACTIVE
    assert by_org["org-b"].status is UserStatus.SUSPENDED
    statuses = {
        p.org_id: p.status
        for p in principals.list()
        if p.user_id in {u.id for u in by_org.values()}
    }
    assert statuses == {
        "org-a": PrincipalStatus.ACTIVE,
        "org-b": PrincipalStatus.DISABLED,
    }
    with system_scope("account"):
        service.selector.select(account, "org-a")
        with pytest.raises(PermissionError):
            service.selector.select(account, "org-b")
    assert _count(owner, "audit_events") == 2


def test_account_rows_are_invisible_to_tenants_and_other_system_scopes(stack):
    harness, restricted, _ = stack
    with system_scope("account"):
        _sign_in(harness, "pat@example.test")
        harness.google.start("/")  # one pending state
    collections = (
        "accounts",
        "account_identities",
        "account_oauth_states",
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
        assert [_count(restricted, name) for name in collections] == [1, 2, 1, 1]
    with system_scope("account"):
        assert [_count(restricted, name) for name in collections] == [1, 2, 1, 1]


def test_without_the_account_scope_the_database_refuses_account_writes(stack):
    harness, _, _ = stack
    with pytest.raises(psycopg.Error):
        harness.google.start("/")


def test_a_lost_identity_race_reuses_the_winner_and_leaves_no_orphan(stack):
    harness, restricted, _ = stack
    service = harness.service
    with system_scope("account"):
        winner, _, _ = _sign_in(harness, "racer@example.test", sub="g-race")
        real_lookup = service.verified_email_identity
        calls = []

        def stale_lookup(email):
            calls.append(email)
            return None if len(calls) == 1 else real_lookup(email)

        service.verified_email_identity = stale_lookup
        loser, _, _ = _sign_in(harness, "racer@example.test", sub="g-race")
        assert loser.id == winner.id
        assert _count(restricted, "accounts") == 1
        assert _count(restricted, "account_identities") == 2


def test_a_conflict_changes_nothing_in_the_database(stack):
    harness, restricted, _ = stack
    with system_scope("account"):
        first, _, _ = _sign_in(harness, "one@example.test", sub="g-one")
        second, _, _ = _sign_in(harness, "two@example.test", sub="g-two")
        before = (
            _count(restricted, "accounts"),
            _count(restricted, "account_identities"),
        )
        with pytest.raises(SignInRefused) as refused:
            _sign_in(harness, "two@example.test", sub="g-one")
        assert refused.value.code == "conflict"
        assert first.id != second.id
        assert before == (
            _count(restricted, "accounts"),
            _count(restricted, "account_identities"),
        )


def test_relinking_is_idempotent_and_keeps_one_membership_per_org(stack):
    harness, restricted, _ = stack
    with system_scope("account"):
        account, _, _ = _sign_in(harness, "pat@example.test")
        assert harness.service.linker.link(account, "pat@example.test") == 0
        assert _count(restricted, "memberships") == 2


def test_session_token_digests_are_unique_in_the_database(stack):
    harness, _, _ = stack
    service = harness.service
    with system_scope("account"):
        account, session, _ = _sign_in(harness, "pat@example.test")
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
    harness, restricted, _ = stack
    service = harness.service
    with system_scope("account"):
        account, session, cookie = _sign_in(harness, "pat@example.test")
        bearer_session, bearer = service.sessions.issue(account, "bearer")
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
