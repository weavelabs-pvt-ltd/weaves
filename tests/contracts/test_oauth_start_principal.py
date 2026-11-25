"""Connector OAuth is authorized for the signed-in person, not a local developer.

Starting a connector authorization used to authenticate the seeded local developer
principal whatever the caller. Production does not seed that principal, so every
connector authorization failed with HTTP 500 there. These tests build the runtime the
way production does (no local seed data) and sign in as real organization members.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from tests.support.account_session import account_headers
from tests.support.organizations import onboard


@pytest.fixture
def production_client(monkeypatch) -> Iterator[TestClient]:
    for name, value in {
        "WEAVES_GITHUB_APP_ID": "123",
        "WEAVES_GITHUB_APP_SLUG": "weaves-test",
        "WEAVES_GITHUB_APP_CLIENT_ID": "client-id",
        "WEAVES_GITHUB_APP_CLIENT_SECRET": "client-secret",
        "WEAVES_GITHUB_APP_PRIVATE_KEY": "test-key",
        "WEAVES_PUBLIC_API_BASE_URL": "https://api.example.test",
    }.items():
        monkeypatch.setenv(name, value)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    try:
        with TestClient(create_app(runtime)) as client:
            yield client
    finally:
        runtime.close()


def _organization(client: TestClient) -> tuple[dict, dict[str, str]]:
    body = onboard(
        client, name="Acme", owner_email="owner@acme.test", owner_display_name="Owner"
    )
    headers = {
        "Authorization": f"Bearer {body['access_token']}",
        "X-Workspace-ID": body["workspace"]["id"],
    }
    return body, headers


def _start_github(client: TestClient, headers: dict[str, str]) -> httpx.Response:
    return client.post(
        "/api/v0/oauth/github/start",
        headers=headers,
        json={"display_name": "Acme GitHub"},
    )


def test_an_owner_can_start_a_connector_authorization_without_local_seed_data(
    production_client,
):
    _body, owner = _organization(production_client)

    response = _start_github(production_client, owner)

    assert response.status_code == 200, response.text
    url = response.json()["authorization_url"]
    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert "redirect_uri=https%3A%2F%2Fapi.example.test%2Fapi%2Fv0%2Foauth" in url


def test_a_member_without_connector_permission_is_refused(production_client):
    body, owner = _organization(production_client)
    roles = production_client.get("/api/v0/roles", headers=owner).json()
    user_role = next(role["id"] for role in roles if role["system_role_key"] == "user")
    created = production_client.post(
        "/api/v0/users",
        headers=owner,
        json={
            "email": "member@acme.test",
            "display_name": "Member",
            "role_id": user_role,
        },
    )
    assert created.status_code == 201, created.text
    member = account_headers(
        production_client,
        "member@acme.test",
        body["organization"]["id"],
        body["workspace"]["id"],
    )

    response = _start_github(production_client, member)

    assert response.status_code == 403, response.text
