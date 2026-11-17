"""Browser cookies stay separate from bearer sessions and require CSRF."""

from http.cookies import SimpleCookie

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from apps.api.app import create_app
from modules.auth.http.browser_sessions import (
    COOKIE_NAME,
    csrf_valid,
    issue_browser_session,
)
from tests.support.runtime import LocalPlatformRuntime


def test_browser_session_requires_same_origin_and_csrf(monkeypatch):
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", "https://app.example.test")
    monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "browser-secret-" * 4)
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    principal = runtime.principals.get("local-developer")
    cookie_response = Response()
    issue_browser_session(runtime.authentication_service, principal, cookie_response)
    header = cookie_response.headers["set-cookie"]
    assert "secure" in header.lower()
    assert "httponly" in header.lower()
    assert "samesite=strict" in header.lower()
    cookie = SimpleCookie()
    cookie.load(header)
    token = cookie[COOKIE_NAME].value

    with TestClient(create_app(runtime), base_url="https://app.example.test") as client:
        client.cookies.set(COOKIE_NAME, token)
        me = client.get("/api/v0/auth/browser/me")
        assert me.status_code == 200, me.text
        csrf = me.json()["csrf_token"]
        assert csrf and "access_token" not in me.json()

        bearer_reuse = client.get(
            "/api/v0/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert bearer_reuse.status_code == 401

        bearer_session, bearer_token = (
            runtime.authentication_service.create_user_session(principal)
        )
        assert bearer_session.channel == "bearer"
        client.cookies.set(COOKIE_NAME, bearer_token)
        assert client.get("/api/v0/auth/browser/me").status_code == 401
        client.cookies.set(COOKIE_NAME, token)

        _assert_request_boundaries(client, csrf)

        logout = client.post(
            "/api/v0/auth/browser/logout",
            headers={"Origin": "https://app.example.test", "X-Weaves-CSRF": csrf},
        )
        assert logout.status_code == 204, logout.text
        deletion = SimpleCookie()
        deletion.load(logout.headers["set-cookie"])
        assert deletion[COOKIE_NAME]["secure"]
        assert deletion[COOKIE_NAME]["httponly"]
        assert deletion[COOKIE_NAME]["samesite"] == "strict"
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/api/v0/auth/browser/me").status_code == 401


def _assert_request_boundaries(client, csrf):
    assert client.post("/api/v0/auth/browser/logout").status_code == 403
    cross_origin = client.post(
        "/api/v0/auth/browser/logout",
        headers={"Origin": "https://attacker.example", "X-Weaves-CSRF": csrf},
    )
    assert cross_origin.status_code == 403
    api_host = client.get(
        "/api/v0/auth/browser/me", headers={"Host": "api.example.test"}
    )
    assert api_host.status_code == 401
    cross_origin_read = client.get(
        "/api/v0/auth/browser/me", headers={"Origin": "https://attacker.example"}
    )
    assert cross_origin_read.status_code == 403
    cross_site_read = client.get(
        "/api/v0/auth/browser/me", headers={"Sec-Fetch-Site": "same-site"}
    )
    assert cross_site_read.status_code == 403
    insecure_read = client.get("http://app.example.test/api/v0/auth/browser/me")
    assert insecure_read.status_code == 401
    wrong_fetch_site = client.post(
        "/api/v0/auth/browser/logout",
        headers={
            "Origin": "https://app.example.test",
            "Sec-Fetch-Site": "cross-site",
            "X-Weaves-CSRF": csrf,
        },
    )
    assert wrong_fetch_site.status_code == 403


@pytest.mark.parametrize("supplied", ["é" * 64, "", "wrong", "0" * 65])
def test_malformed_csrf_is_rejected_without_exception(monkeypatch, supplied):
    monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "browser-secret-" * 4)
    assert not csrf_valid("session", supplied)
