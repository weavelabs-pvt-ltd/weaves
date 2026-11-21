"""Account browser sessions: cookie + CSRF, isolated from bearer sessions."""

from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import AccountStatus, MembershipStatus
from modules.auth.account_sessions import token_digest
from modules.auth.http.browser_sessions import ACCOUNT_COOKIE_NAME, csrf_token
from shared.clock import utc_now
from tests.support.account_session import configure_google, google_sign_in, sign_in
from tests.support.fake_google import FakeGoogle
from tests.support.organizations import create_organization

ORIGIN = "https://app.example.test"
EMAIL = "browser.person@example.test"
SAME = {"Origin": ORIGIN}


@pytest.fixture
def world(monkeypatch):
    configure_google(monkeypatch)
    fake = FakeGoogle()
    runtime = compose_platform_runtime(
        http_client=fake.client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    service = runtime.account_sign_in_service
    org_a, _, _, principal_a, owner_a = create_organization(
        runtime, name="Org A", email=EMAIL, display_name="Person A"
    )
    org_b, *_ = create_organization(
        runtime,
        name="Org B",
        email="someone.else@example.test",
        display_name="Other",
    )
    client = TestClient(
        create_app(runtime), base_url=ORIGIN, raise_server_exceptions=False
    )

    def sign_in():
        return google_sign_in(client, fake, EMAIL)

    with client:
        yield {
            "runtime": runtime,
            "service": service,
            "client": client,
            "sign_in": sign_in,
            "org_a": org_a,
            "org_b": org_b,
            "owner_a": owner_a,
            "principal_a": principal_a,
            "fake": fake,
        }
    runtime.close()


def _login(world):
    response = world["sign_in"]()
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/"
    token = world["client"].cookies.get(ACCOUNT_COOKIE_NAME)
    return csrf_token(token), token


def _headers(csrf, org=None):
    headers = {"Origin": ORIGIN, "X-Weaves-CSRF": csrf}
    if org:
        headers["X-Organization-Id"] = org
    return headers


def test_callback_sets_a_distinct_secure_cookie_and_the_me_route_gives_csrf(world):
    response = world["sign_in"]()
    assert response.status_code == 302, response.text
    cookies = {}
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        cookies.update(cookie)
    morsel = cookies[ACCOUNT_COOKIE_NAME]
    assert morsel["secure"] and morsel["httponly"]
    assert morsel["samesite"] == "strict" and morsel["path"] == "/"
    assert int(morsel["max-age"]) == 12 * 3600
    assert response.headers["cache-control"] == "no-store"
    assert morsel.value.startswith("wvs_acbr_")
    session = world["service"].sessions._sessions.find_by_field(
        "token_digest",
        token_digest(morsel.value),
    )
    assert (
        session.channel == "browser" and morsel.value not in session.model_dump_json()
    )
    me = world["client"].get("/api/v0/account/browser/me").json()
    assert me["csrf_token"] == csrf_token(morsel.value) and "access_token" not in me
    assert {m["organization_id"] for m in me["memberships"]} == {world["org_a"].id}


def test_me_is_cookie_only_and_csrf_is_not_required_for_safe_methods(world):
    csrf, token = _login(world)
    client = world["client"]
    me = client.get("/api/v0/account/browser/me")
    assert me.status_code == 200 and me.json()["csrf_token"] == csrf
    assert me.headers["cache-control"] == "no-store"
    # No cookie: refused. Bearer header alongside the cookie: refused.
    client.cookies.clear()
    assert client.get("/api/v0/account/browser/me").status_code == 401
    client.cookies.set(ACCOUNT_COOKIE_NAME, token)
    mixed = client.get(
        "/api/v0/account/browser/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert mixed.status_code == 401
    host = client.get(
        "/api/v0/account/browser/me", headers={"Host": "api.example.test"}
    )
    assert host.status_code == 401


def test_unsafe_methods_need_csrf_and_an_allowed_origin(world):
    csrf, _ = _login(world)
    client = world["client"]
    path = "/api/v0/account/browser/logout"
    assert client.post(path).status_code == 403
    assert client.post(path, headers={"Origin": ORIGIN}).status_code == 403
    assert client.post(path, headers={"X-Weaves-CSRF": csrf}).status_code == 403
    bad = client.post(path, headers=_headers("0" * 64))
    assert bad.status_code == 403
    cross = client.post(path, headers={**_headers(csrf), "Origin": "https://evil.test"})
    assert cross.status_code == 403
    assert client.get("/api/v0/account/browser/me").status_code == 200  # still live


def test_logout_revokes_the_session_and_clears_the_cookie(world):
    csrf, token = _login(world)
    client = world["client"]
    out = client.post("/api/v0/account/browser/logout", headers=_headers(csrf))
    assert out.status_code == 204
    cleared = SimpleCookie()
    cleared.load(out.headers["set-cookie"])
    morsel = cleared[ACCOUNT_COOKIE_NAME]
    assert morsel["secure"] and morsel["httponly"] and morsel["samesite"] == "strict"
    assert morsel["max-age"] == "0"
    client.cookies.set(ACCOUNT_COOKIE_NAME, token)  # replaying the old cookie fails
    assert client.get("/api/v0/account/browser/me").status_code == 401
    with pytest.raises(PermissionError):
        world["service"].sessions.authenticate(token, "browser")


def test_each_verify_rotates_the_session(world):
    _, first = _login(world)
    _, second = _login(world)  # same browser, old cookie presented
    assert first != second
    client = world["client"]
    assert client.get("/api/v0/account/browser/me").status_code == 200
    client.cookies.set(ACCOUNT_COOKIE_NAME, first)
    assert client.get("/api/v0/account/browser/me").status_code == 401


def test_cookie_works_on_tenant_routes_only_with_a_valid_organization(world):
    csrf, _ = _login(world)
    client, org_a, org_b = world["client"], world["org_a"], world["org_b"]
    ok = client.get("/api/v0/agents", headers={"X-Organization-Id": org_a.id})
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert client.get("/api/v0/agents").status_code == 401
    unknown = client.get("/api/v0/agents", headers={"X-Organization-Id": "nope"})
    foreign = client.get("/api/v0/agents", headers={"X-Organization-Id": org_b.id})
    assert unknown.status_code == 403
    assert (unknown.status_code, unknown.json()) == (
        foreign.status_code,
        foreign.json(),
    )
    # unsafe tenant call: CSRF + Origin are enforced, then the call is served
    body = {"name": "Cookie agent", "instructions": "Say hello"}
    assert client.post("/api/v0/agents", json=body).status_code == 403
    no_csrf = client.post(
        "/api/v0/agents", json=body, headers={**SAME, "X-Organization-Id": org_a.id}
    )
    assert no_csrf.status_code == 403
    created = client.post("/api/v0/agents", json=body, headers=_headers(csrf, org_a.id))
    assert created.status_code in (200, 201), created.text
    runtime = world["runtime"]
    assert any(
        event.actor_principal_id == world["principal_a"].id
        for event in runtime.audit_events.list()
    )


def test_membership_state_is_rechecked_through_the_cookie_path(world):
    _login(world)
    runtime, client, org_a = world["runtime"], world["client"], world["org_a"]
    path = ("/api/v0/agents", {"X-Organization-Id": org_a.id})
    assert client.get(path[0], headers=path[1]).status_code == 200
    membership = runtime.memberships.list_scoped(org_a.id)[0]
    runtime.memberships.put(
        membership.model_copy(update={"status": MembershipStatus.REMOVED})
    )
    assert client.get(path[0], headers=path[1]).status_code == 403


def test_bearer_and_cookie_credentials_are_not_interchangeable(world):
    service, client = world["service"], world["client"]
    _, cookie_token = _login(world)
    _, bearer = sign_in(service, EMAIL)
    org = {"X-Organization-Id": world["org_a"].id}
    client.cookies.clear()

    # a browser token presented as a bearer fails on account and tenant routes
    as_bearer = {"Authorization": f"Bearer {cookie_token}"}
    assert client.get("/api/v0/account/me", headers=as_bearer).status_code == 401
    assert client.get("/api/v0/agents", headers={**as_bearer, **org}).status_code == 401
    # a bearer token presented as the cookie fails on cookie routes
    client.cookies.set(ACCOUNT_COOKIE_NAME, bearer)
    assert client.get("/api/v0/account/browser/me").status_code == 401
    assert client.get("/api/v0/agents", headers=org).status_code == 403
    # a cookie never authenticates the bearer-only account routes
    client.cookies.set(ACCOUNT_COOKIE_NAME, cookie_token)
    assert client.get("/api/v0/account/me").status_code == 401
    assert client.post("/api/v0/account/logout").status_code == 401
    # and the bearer still works as a bearer
    ok = client.get("/api/v0/account/me", headers={"Authorization": f"Bearer {bearer}"})
    assert ok.status_code == 200


def test_an_unknown_cookie_is_refused_everywhere(world):
    _login(world)
    client = world["client"]
    org = {"X-Organization-Id": world["org_a"].id}
    forged = "wvs_acbr_" + "x" * 43
    csrf = csrf_token(forged)
    client.cookies.clear()
    client.cookies.set(ACCOUNT_COOKIE_NAME, forged)
    assert client.get("/api/v0/account/browser/me").status_code == 401
    assert client.get("/api/v0/agents", headers=org).status_code == 403
    out = client.post("/api/v0/account/browser/logout", headers=_headers(csrf))
    assert out.status_code == 401
    # the removed tenant browser routes no longer exist behind any cookie
    assert client.get("/api/v0/auth/browser/me").status_code in (401, 404)


def test_a_disabled_account_loses_the_session_at_once_and_cannot_sign_in(world):
    _login(world)
    runtime, client = world["runtime"], world["client"]
    org = {"X-Organization-Id": world["org_a"].id}
    assert client.get("/api/v0/account/browser/me").status_code == 200
    account = runtime.accounts.list()[0]
    runtime.accounts.put(account.model_copy(update={"status": AccountStatus.DISABLED}))
    assert client.get("/api/v0/account/browser/me").status_code == 401
    assert client.get("/api/v0/agents", headers=org).status_code == 403
    client.cookies.clear()
    refused = world["sign_in"]()
    assert refused.status_code == 302
    assert refused.headers["location"] == "/#account-signin?error=disabled"
    assert ACCOUNT_COOKIE_NAME not in client.cookies


def test_an_expired_browser_session_is_refused(world):
    _login(world)
    store = world["service"].sessions._sessions
    session = store.list()[0]
    store.put(
        session.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    assert world["client"].get("/api/v0/account/browser/me").status_code == 401
