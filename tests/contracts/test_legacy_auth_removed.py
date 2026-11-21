"""Only the account flow, service credentials and the static token authenticate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.account_session import account_headers
from tests.support.organizations import onboard
from tests.support.runtime import LocalPlatformRuntime

STATIC = "static-development-token"

REMOVED = (
    ("POST", "/api/v0/auth/login"),
    ("POST", "/api/v0/auth/email-code"),
    ("POST", "/api/v0/auth/email-verify"),
    ("PUT", "/api/v0/auth/password"),
    ("POST", "/api/v0/auth/logout"),
    ("GET", "/api/v0/auth/browser/me"),
    ("POST", "/api/v0/auth/browser/logout"),
    ("GET", "/api/v0/auth/sso/providers"),
    ("GET", "/api/v0/auth/sso/google/start"),
    ("GET", "/api/v0/auth/sso/google/callback"),
    ("POST", "/api/v0/onboarding/organizations"),
    ("POST", "/api/v0/auth/accept-invitation"),
)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("WEAVES_API_TOKEN", STATIC)
    monkeypatch.delenv("WEAVES_API_TOKENS", raising=False)
    runtime = LocalPlatformRuntime(postgres_store=None)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        yield runtime, client
    runtime.close()


def test_removed_routes_do_not_exist_even_for_an_authenticated_caller(world):
    _, client = world
    headers = {"Authorization": f"Bearer {STATIC}"}
    for method, path in REMOVED:
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code in (404, 405), (method, path, response.status_code)
        public = client.request(method, path, json={})
        assert public.status_code == 401, (method, path)
    documented = set(client.get("/openapi.json", headers=headers).json()["paths"])
    assert not {path for _, path in REMOVED} & documented


@pytest.mark.parametrize(
    "headers,cookies",
    [
        ({}, {}),
        ({"Authorization": "Bearer not-a-real-token"}, {}),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, {}),
        ({"Authorization": "Bearer wvs_acct_" + "x" * 43}, {}),
        ({"Authorization": "Bearer " + "a" * 43}, {"__Host-weaves_session": "x" * 43}),
        ({}, {"__Host-weaves_session": "x" * 43}),
        ({}, {"weaves_sso": "x" * 43}),
    ],
)
def test_every_other_credential_is_unauthorized(world, headers, cookies):
    _, client = world
    client.cookies.clear()
    for name, value in cookies.items():
        client.cookies.set(name, value)
    response = client.get("/api/v0/auth/me", headers=headers)
    assert response.status_code == 401


def test_accepted_credentials_each_resolve_a_principal(world):
    _, client = world
    static = client.get(
        "/api/v0/auth/me", headers={"Authorization": f"Bearer {STATIC}"}
    )
    assert static.status_code == 200 and static.json()["principal"]["id"]
    owner = onboard(
        client, name="Accepted", owner_email="o@accepted.test", owner_display_name="O"
    )
    service = {"Authorization": f"Bearer {owner['access_token']}"}
    assert client.get("/api/v0/auth/me", headers=service).status_code == 200
    account = account_headers(client, "o@accepted.test", owner["organization"]["id"])
    me = client.get("/api/v0/auth/me", headers=account)
    assert me.status_code == 200 and me.json()["user"]["id"] == owner["owner"]["id"]
    without_org = {"Authorization": account["Authorization"]}
    assert client.get("/api/v0/auth/me", headers=without_org).status_code == 401
    assert (
        client.get(
            "/api/v0/auth/me", headers={**account, "X-Organization-Id": "x"}
        ).status_code
        == 403
    )
