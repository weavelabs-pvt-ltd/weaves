"""OIDC signature, browser state, identity mapping and provisioned access checks."""

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


@pytest.fixture
def signin(monkeypatch):
    monkeypatch.setenv("WEAVES_SSO_SESSION_SECRET", "isolated-session-secret-" * 3)
    monkeypatch.setenv("WEAVES_SSO_GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("WEAVES_SSO_GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", "http://localhost:8080")
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    issuer = "https://accounts.google.com"
    claims = {
        "iss": issuer,
        "aud": "test-client",
        "sub": "google-subject",
        "email": "developer@localhost",
        "email_verified": True,
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }

    def provider(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "jwks_uri": issuer + "/keys",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path == "/token":
            token = jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key)
            return httpx.Response(
                200,
                json={
                    "access_token": "provider-token",
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "id_token": token,
                },
            )
        raise AssertionError(str(request.url))

    with TestClient(create_app(runtime), follow_redirects=False) as client:
        remote = client.app.state.signin_oauth.create_client("google")
        remote.client_kwargs["transport"] = httpx.MockTransport(provider)
        yield client, runtime, claims


def start(client, claims):
    response = client.get("/api/v0/auth/sso/google/start")
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"]
    assert "httponly" in response.headers["set-cookie"].lower()
    claims["nonce"] = query["nonce"][0]
    return query["state"][0]


def test_verified_oidc_login_issues_normal_revocable_session(signin):
    client, runtime, claims = signin
    state = start(client, claims)
    response = client.get(
        "/api/v0/auth/sso/google/callback", params={"code": "code", "state": state}
    )
    assert '"status": "success"' in response.text, response.text
    assert '"access_token"' in response.text
    assert len(runtime.user_sessions.list()) == 1
    assert response.headers["cache-control"] == "no-store"
    assert "http://localhost:8080" in response.text
    replay = client.get(
        "/api/v0/auth/sso/google/callback", params={"code": "code", "state": state}
    )
    assert '"status": "failed"' in replay.text
    assert len(runtime.user_sessions.list()) == 1


@pytest.mark.parametrize("invalid", ["nonce", "aud", "exp", "email_verified", "email"])
def test_invalid_provider_identity_does_not_issue_session(signin, invalid):
    client, runtime, claims = signin
    state = start(client, claims)
    claims[invalid] = {
        "nonce": "wrong",
        "aud": "another-app",
        "exp": 1,
        "email_verified": False,
        "email": "unprovisioned@example.com",
    }[invalid]
    response = client.get(
        "/api/v0/auth/sso/google/callback", params={"code": "code", "state": state}
    )
    assert '"status": "failed"' in response.text
    assert not runtime.user_sessions.list()


def test_callback_without_browser_state_is_rejected(signin):
    client, runtime, _ = signin
    response = client.get(
        "/api/v0/auth/sso/google/callback", params={"code": "code", "state": "forged"}
    )
    assert '"status": "failed"' in response.text
    assert not runtime.user_sessions.list()


def test_unconfigured_provider_reports_a_real_configuration_failure(signin):
    client, _, _ = signin
    response = client.get("/api/v0/auth/sso/zoho/start")
    assert "SSO-NOT-CONFIGURED" in response.text
    assert '"access_token"' not in response.text
