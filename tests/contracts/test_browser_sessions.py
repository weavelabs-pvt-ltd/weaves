"""Browser cookies stay separate from bearer sessions and require CSRF."""

from http.cookies import SimpleCookie

from fastapi import Response
from fastapi.testclient import TestClient

from apps.api.app import create_app
from modules.auth.http.browser_sessions import COOKIE_NAME, issue_browser_session
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

        missing_csrf = client.post("/api/v0/auth/browser/logout")
        assert missing_csrf.status_code == 403
        cross_origin = client.post(
            "/api/v0/auth/browser/logout",
            headers={"Origin": "https://attacker.example", "X-Weaves-CSRF": csrf},
        )
        assert cross_origin.status_code == 403
        api_host = client.get(
            "/api/v0/auth/browser/me", headers={"Host": "api.example.test"}
        )
        assert api_host.status_code == 401
        wrong_fetch_site = client.post(
            "/api/v0/auth/browser/logout",
            headers={
                "Origin": "https://app.example.test",
                "Sec-Fetch-Site": "cross-site",
                "X-Weaves-CSRF": csrf,
            },
        )
        assert wrong_fetch_site.status_code == 403

        logout = client.post(
            "/api/v0/auth/browser/logout",
            headers={"Origin": "https://app.example.test", "X-Weaves-CSRF": csrf},
        )
        assert logout.status_code == 204, logout.text
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/api/v0/auth/browser/me").status_code == 401
