"""Display-name edits, identity removal, CORS and the removed email-code routes."""

from __future__ import annotations

import pytest

from tests.support.account_session import ORIGIN, google_headers, sign_in
from tests.support.google_world import EMAIL, google_world

__all__ = ["google_world"]

REMOVED = (
    "/api/v0/account/email-code",
    "/api/v0/account/email-verify",
    "/api/v0/account/browser/email-verify",
    "/api/v0/account/identities/email-code",
    "/api/v0/account/identities/email-verify",
)


def _bearer(world):
    account, token = sign_in(world.service, EMAIL)
    return account, {"Authorization": f"Bearer {token}"}


def test_the_email_code_routes_are_gone_and_not_documented(google_world):
    world = google_world()
    paths = world.client.get("/openapi.json").json()["paths"]
    assert not any("email-" in path for path in paths)
    _, bearer = _bearer(world)
    for path in REMOVED:
        assert path not in paths, path
        client = world.client
        # Authenticated callers reach the router and find nothing there.
        cookie = google_headers(client, world.fake, EMAIL)
        credentials = cookie if "/browser/" in path else bearer
        for method in ("post", "get", "put", "delete"):
            response = getattr(client, method)(path, headers=credentials)
            assert response.status_code in (404, 405), (method, path)
        # Anonymous callers are never served either.
        client.cookies.clear()
        anonymous = client.post(path, json={"email": EMAIL})
        assert anonymous.status_code in (401, 404, 405), path


def test_the_bearer_patch_renames_the_account(google_world):
    world = google_world()
    account, headers = _bearer(world)
    done = world.client.patch(
        "/api/v0/account/me", json={"display_name": "  Dr. Pat  "}, headers=headers
    )
    assert done.status_code == 200
    assert done.json() == {"account": {"id": account.id, "display_name": "Dr. Pat"}}
    me = world.client.get("/api/v0/account/me", headers=headers).json()
    assert me["account"]["display_name"] == "Dr. Pat"
    assert world.runtime.accounts.get(account.id).display_name == "Dr. Pat"


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "x" * 161},
        {"display_name": "line\nbreak"},
        {"display_name": "bell\x07"},
        {"display_name": 12},
        {},
        {"display_name": "ok", "status": "disabled"},
    ],
)
def test_invalid_display_names_are_rejected_on_both_routes(google_world, body):
    world = google_world()
    _, bearer = _bearer(world)
    assert (
        world.client.patch("/api/v0/account/me", json=body, headers=bearer).status_code
        == 422
    )
    headers = google_headers(world.client, world.fake, EMAIL)
    cookie = world.client.patch(
        "/api/v0/account/browser/me", json=body, headers=headers
    )
    assert cookie.status_code == 422
    assert all(a.display_name != "x" * 161 for a in world.runtime.accounts.list())


def test_the_longest_name_is_accepted(google_world):
    world = google_world()
    _, headers = _bearer(world)
    name = "n" * 160
    ok = world.client.patch(
        "/api/v0/account/me", json={"display_name": name}, headers=headers
    )
    assert ok.status_code == 200 and ok.json()["account"]["display_name"] == name


def test_the_cookie_patch_needs_csrf_origin_and_a_session(google_world):
    world = google_world()
    headers = google_headers(world.client, world.fake, EMAIL)
    url = "/api/v0/account/browser/me"
    body = {"display_name": "Cookie Name"}
    assert world.client.patch(url, json=body).status_code == 403
    assert (
        world.client.patch(url, json=body, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    no_origin = {k: v for k, v in headers.items() if k != "Origin"}
    assert world.client.patch(url, json=body, headers=no_origin).status_code == 403
    cross = {**headers, "Origin": "https://evil.test"}
    assert world.client.patch(url, json=body, headers=cross).status_code == 403
    wrong_csrf = {**headers, "X-Weaves-CSRF": "0" * 64}
    assert world.client.patch(url, json=body, headers=wrong_csrf).status_code == 403
    assert world.runtime.accounts.list()[0].display_name != "Cookie Name"

    done = world.client.patch(url, json=body, headers=headers)
    assert done.status_code == 200
    assert done.json()["account"]["display_name"] == "Cookie Name"
    assert done.headers["cache-control"] == "no-store"
    assert world.client.get(url).json()["account"]["display_name"] == "Cookie Name"

    world.client.cookies.clear()
    assert world.client.patch(url, json=body, headers=headers).status_code == 401


def test_the_bearer_patch_refuses_cookies_and_missing_sessions(google_world):
    world = google_world()
    headers = google_headers(world.client, world.fake, EMAIL)
    body = {"display_name": "Nope"}
    assert world.client.patch("/api/v0/account/me", json=body).status_code == 401
    cookie_only = world.client.patch("/api/v0/account/me", json=body, headers=headers)
    assert cookie_only.status_code == 401


def test_the_last_verified_identity_cannot_be_removed(google_world):
    world = google_world()
    google_headers(world.client, world.fake, EMAIL)
    account = world.runtime.accounts.list()[0]
    _, token = world.service.sessions.issue(account, "bearer")
    bearer = {"Authorization": f"Bearer {token}"}
    me = world.client.get("/api/v0/account/me", headers=bearer).json()
    assert {i["kind"] for i in me["identities"]} == {"oidc", "email"}
    assert all(i["subject"] != EMAIL for i in me["identities"])
    first, second = (i["id"] for i in me["identities"])
    assert (
        world.client.delete(
            f"/api/v0/account/identities/{first}", headers=bearer
        ).status_code
        == 204
    )
    last = world.client.delete(f"/api/v0/account/identities/{second}", headers=bearer)
    assert last.status_code == 409
    assert (
        world.client.delete(
            "/api/v0/account/identities/nope", headers=bearer
        ).status_code
        == 404
    )


def test_another_accounts_identity_is_not_found(google_world):
    world = google_world()
    _, bearer = _bearer(world)
    other, _ = sign_in(world.service, "other@example.test")
    theirs = world.runtime.account_identities.list_by_field("account_id", other.id)[0]
    gone = world.client.delete(
        f"/api/v0/account/identities/{theirs.id}", headers=bearer
    )
    assert gone.status_code == 404
    assert world.runtime.account_identities.get(theirs.id)


def test_cors_allows_the_organization_and_csrf_headers_and_exposes_retry_after(
    google_world,
):
    world = google_world()
    origin = "http://localhost:5173"
    preflight = world.client.options(
        "/api/v0/account/browser/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": (
                "x-organization-id, x-weaves-csrf, content-type"
            ),
        },
    )
    assert preflight.status_code == 200
    allowed = preflight.headers["access-control-allow-headers"].lower()
    for header in ("x-organization-id", "x-weaves-csrf", "content-type"):
        assert header in allowed
    assert "x-weaves-onboarding-token" not in allowed
    assert "PATCH" in preflight.headers["access-control-allow-methods"]
    retired = world.client.options(
        "/api/v0/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-weaves-onboarding-token",
        },
    )
    assert retired.status_code == 400
    simple = world.client.get("/api/v0/health", headers={"Origin": origin})
    assert simple.headers["access-control-expose-headers"] == "Retry-After"
