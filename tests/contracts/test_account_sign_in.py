"""Account directory, membership linking and bearer account sessions (memory)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    AccountStatus,
    MembershipStatus,
    OrganizationMembership,
    PrincipalType,
    UserStatus,
)
from shared.clock import utc_now
from tests.support.account_world import seed_member, seed_organization
from tests.support.organizations import onboard
from tests.support.runtime import LocalPlatformRuntime


@pytest.fixture
def world():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    service = runtime.account_sign_in_service
    for org_id in ("org-a", "org-b", "org-c"):
        seed_organization(runtime.organizations, org_id)
    yield runtime, service
    runtime.close()


def _sign_in(service, email):
    """Resolve the account for a verified email and issue a bearer session."""
    normalized = email.strip().casefold()
    account = service.find_or_create_account(normalized)
    service.linker.link(account, normalized)
    session, token = service.sessions.issue(account)
    return account, session, token


def _member(runtime, org_id, email, **kwargs):
    return seed_member(runtime.users, runtime.principals, org_id, email, **kwargs)


def _memberships(runtime, account):
    return {
        m.org_id: m for m in runtime.memberships.list() if m.account_id == account.id
    }


def test_first_sign_in_creates_a_verified_account_and_second_reuses_it(world):
    runtime, service = world
    first, session, token = _sign_in(service, "  New.Person@Example.Test ")
    second, _, _ = _sign_in(service, "new.person@example.test")
    assert second.id == first.id
    assert len(runtime.accounts.list()) == 1
    (identity,) = runtime.account_identities.list()
    assert identity.subject == "new.person@example.test"
    assert identity.kind is AccountIdentityKind.EMAIL
    assert identity.verified_at is not None
    assert token.startswith("wvs_acct_")
    assert session.revoked_at is None
    stored = runtime.account_sessions.get(session.id)
    assert len(stored.token_digest) == 64 and token not in stored.model_dump_json()
    assert stored.expires_at - stored.created_at == timedelta(hours=12)


def test_same_email_in_two_organizations_is_one_account_with_two_memberships(world):
    runtime, service = world
    _member(runtime, "org-a", "shared@example.test")
    _member(runtime, "org-b", "Shared@Example.Test", status=UserStatus.INVITED)
    other, _ = _member(runtime, "org-c", "someone.else@example.test")
    account, _, _ = _sign_in(service, "shared@example.test")
    linked = _memberships(runtime, account)
    assert set(linked) == {"org-a", "org-b"}
    assert all(m.status is MembershipStatus.ACTIVE for m in linked.values())
    assert len(runtime.accounts.list()) == 1
    assert other.id not in {m.user_id for m in runtime.memberships.list()}
    again, _, _ = _sign_in(service, "shared@example.test")
    assert again.id == account.id and len(runtime.memberships.list()) == 2


def test_different_emails_never_merge_accounts_or_memberships(world):
    runtime, service = world
    _member(runtime, "org-a", "one@example.test", user_id="u1")
    _member(runtime, "org-a", "one+tag@example.test", user_id="u2")
    one, _, _ = _sign_in(service, "one@example.test")
    two, _, _ = _sign_in(service, "one+tag@example.test")
    assert one.id != two.id
    assert {
        m.user_id for m in runtime.memberships.list() if m.account_id == one.id
    } == {"u1"}
    # The second address shares the organization but its user is separate.
    assert {
        m.user_id for m in runtime.memberships.list() if m.account_id == two.id
    } == {"u2"}


def test_removed_and_suspended_memberships_are_never_resurrected(world):
    runtime, service = world
    for org in ("org-a", "org-b", "org-c"):
        _member(runtime, org, "member@example.test")
    account, _, _ = _sign_in(service, "member@example.test")
    before = _memberships(runtime, account)
    for org, status in (
        ("org-a", MembershipStatus.REMOVED),
        ("org-b", MembershipStatus.SUSPENDED),
    ):
        runtime.memberships.put(
            OrganizationMembership.model_validate(
                before[org].model_copy(update={"status": status}).model_dump()
            )
        )
    _sign_in(service, "member@example.test")
    after = _memberships(runtime, account)
    assert {org: m.status for org, m in after.items()} == {
        "org-a": MembershipStatus.REMOVED,
        "org-b": MembershipStatus.SUSPENDED,
        "org-c": MembershipStatus.ACTIVE,
    }
    assert len(runtime.memberships.list()) == 3


def test_ineligible_tenant_users_are_not_linked(world):
    runtime, service = world
    _member(runtime, "org-a", "gone@example.test", status=UserStatus.DELETED)
    _member(runtime, "org-b", "gone@example.test", status=UserStatus.SUSPENDED)
    _member(
        runtime,
        "org-c",
        "gone@example.test",
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )
    seed_member(runtime.users, runtime.principals, "no-such-org", "gone@example.test")
    account, _, _ = _sign_in(service, "gone@example.test")
    assert _memberships(runtime, account) == {}


def test_user_without_any_membership_still_signs_in_with_an_empty_list(world):
    _, service = world
    account, _, token = _sign_in(service, "nobody@example.test")
    assert service.sessions.authenticate(token).id == account.id


def test_disabled_account_is_refused_and_existing_sessions_stop(world):
    runtime, service = world
    account, _, token = _sign_in(service, "disabled@example.test")
    runtime.accounts.put(
        Account.model_validate(
            account.model_copy(update={"status": AccountStatus.DISABLED}).model_dump()
        )
    )
    with pytest.raises(PermissionError):
        service.sessions.authenticate(token)
    with pytest.raises(PermissionError):
        service.sessions.issue(service.accounts.get(account.id))
    assert len(runtime.account_sessions.list()) == 1  # no new session was issued


def test_unverified_identity_never_authenticates_its_account(world):
    runtime, service = world
    _member(runtime, "org-a", "claimed@example.test")
    now = utc_now()
    runtime.accounts.create(
        Account(
            id="planted",
            display_name="Planted",
            status=AccountStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    runtime.account_identities.create(
        AccountIdentity(
            id="planted-identity",
            account_id="planted",
            kind=AccountIdentityKind.EMAIL,
            subject="claimed@example.test",
            verified_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    account, _, _ = _sign_in(service, "claimed@example.test")
    assert account.id != "planted"
    assert _memberships(runtime, account).keys() == {"org-a"}
    assert not [m for m in runtime.memberships.list() if m.account_id == "planted"]


# ---- HTTP -----------------------------------------------------------------


@pytest.fixture
def client(world):
    runtime, service = world
    with TestClient(create_app(runtime)) as http:
        yield http, runtime, service


def _http_sign_in(service, email):
    """Issue a bearer session and describe it like a sign-in response."""
    account, _, token = _sign_in(service, email)
    return {
        "access_token": token,
        "account": {
            "id": account.id,
            "display_name": account.display_name,
            "role_title": account.role_title,
        },
    }


def _bearer(body):
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_http_me_returns_account_identities_and_memberships(client):
    http, runtime, service = client
    _member(runtime, "org-a", "person@example.test")
    _member(runtime, "org-b", "person@example.test")
    body = _http_sign_in(service, "Person@Example.test")
    assert body["access_token"].startswith("wvs_acct_")
    assert set(body["account"]) == {"id", "display_name", "role_title"}
    me = http.get("/api/v0/account/me", headers=_bearer(body))
    memberships = me.json()["memberships"]
    assert {m["organization_id"] for m in memberships} == {"org-a", "org-b"}
    assert {m["organization_name"] for m in memberships} == {"Org org-a", "Org org-b"}
    assert all(m["membership_status"] == "active" for m in memberships)
    me = http.get("/api/v0/account/me", headers=_bearer(body))
    assert me.status_code == 200
    assert me.json()["account"] == body["account"]
    assert me.json()["identities"] == [
        {
            "id": me.json()["identities"][0]["id"],
            "kind": "email",
            "subject": "p***@example.test",
            "verified_at": me.json()["identities"][0]["verified_at"],
        }
    ]
    assert "person@example.test" not in me.text
    assert {m["organization_status"] for m in me.json()["memberships"]} == {"active"}
    assert all(m["created_at"] for m in me.json()["memberships"])


def test_me_lists_active_and_suspended_only_and_never_another_accounts(client):
    http, runtime, service = client
    for org in ("org-a", "org-b", "org-c"):
        _member(runtime, org, "mine@example.test")
    _member(runtime, "org-a", "theirs@example.test")
    _member(runtime, "org-b", "theirs-too@example.test")
    mine = _http_sign_in(service, "mine@example.test")
    theirs = _http_sign_in(service, "theirs@example.test")
    _http_sign_in(service, "theirs-too@example.test")
    by_org = {m.org_id: m for m in runtime.memberships.list()}
    mine_id = mine["account"]["id"]
    owned = {m.org_id: m for m in runtime.memberships.list() if m.account_id == mine_id}
    for org, status in (
        ("org-b", MembershipStatus.SUSPENDED),
        ("org-c", MembershipStatus.REMOVED),
    ):
        runtime.memberships.put(
            OrganizationMembership.model_validate(
                owned[org].model_copy(update={"status": status}).model_dump()
            )
        )
    assert by_org
    rows = http.get("/api/v0/account/me", headers=_bearer(mine)).json()["memberships"]
    assert {(r["organization_id"], r["membership_status"]) for r in rows} == {
        ("org-a", "active"),
        ("org-b", "suspended"),
    }
    theirs_rows = http.get("/api/v0/account/me", headers=_bearer(theirs)).json()
    assert [r["organization_id"] for r in theirs_rows["memberships"]] == ["org-a"]


def test_account_and_tenant_tokens_never_cross(client):
    http, runtime, service = client
    onboarding = onboard(
        http,
        name="Isolation Org",
        owner_email="owner@isolation.test",
        owner_display_name="Owner",
    )
    tenant_headers = {"Authorization": f"Bearer {onboarding['access_token']}"}
    account = _http_sign_in(service, "owner@isolation.test")
    account_headers = _bearer(account)

    assert http.get("/api/v0/account/me", headers=tenant_headers).status_code == 401
    assert http.get("/api/v0/account/me").status_code == 401
    assert (
        http.post("/api/v0/account/logout", headers=tenant_headers).status_code == 401
    )
    for path in ("/api/v0/auth/me", "/api/v0/workspaces", "/api/v0/agents"):
        assert http.get(path, headers=tenant_headers).status_code == 200, path
        assert http.get(path, headers=account_headers).status_code == 401, path
    assert (
        http.get(
            "/api/v0/account/me",
            headers={**account_headers, "X-Workspace-ID": "anything"},
        ).status_code
        == 200
    )
    assert len(runtime.account_sessions.list()) == 1


def test_logout_revokes_the_session(client):
    http, runtime, service = client
    body = _http_sign_in(service, "bye@example.test")
    other = _http_sign_in(service, "bye@example.test")
    assert http.post("/api/v0/account/logout", headers=_bearer(body)).status_code == 204
    assert http.get("/api/v0/account/me", headers=_bearer(body)).status_code == 401
    assert http.post("/api/v0/account/logout", headers=_bearer(body)).status_code == 401
    assert http.get("/api/v0/account/me", headers=_bearer(other)).status_code == 200
    assert sum(s.revoked_at is not None for s in runtime.account_sessions.list()) == 1


def test_expired_session_is_rejected(client):
    http, runtime, service = client
    body = _http_sign_in(service, "old@example.test")
    (session,) = runtime.account_sessions.list()
    runtime.account_sessions.put(
        session.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    assert http.get("/api/v0/account/me", headers=_bearer(body)).status_code == 401
