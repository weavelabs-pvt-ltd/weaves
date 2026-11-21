"""Cookie-backed account sessions create organizations with CSRF protection."""

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from modules.auth.http.browser_sessions import ACCOUNT_COOKIE_NAME
from tests.support.account_org_support import ENABLE, prepare

ORIGIN = "https://app.example.test"
EMAIL = "browser.founder@example.test"
URL = "/api/v0/account/browser/organizations"
VERIFY = "/api/v0/account/browser/email-verify"


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv(ENABLE, "true")
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", ORIGIN)
    monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "browser-secret-" * 4)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    signer = prepare(runtime)
    client = TestClient(
        create_app(runtime), base_url=ORIGIN, raise_server_exceptions=False
    )
    service = runtime.account_sign_in_service
    challenge = service.start_email_login(EMAIL, "198.51.100.1")
    with client:
        verified = client.post(
            VERIFY,
            json={"challenge_id": challenge, "code": signer.mail.code()},
            headers={"Origin": ORIGIN},
        )
        assert verified.status_code == 200, verified.text
        yield {
            "runtime": runtime,
            "client": client,
            "csrf": verified.json()["csrf_token"],
            "signer": signer,
        }
    runtime.close()


def _headers(world, key="cookie-key", **extra):
    return {
        "Origin": ORIGIN,
        "X-Weaves-CSRF": world["csrf"],
        "Idempotency-Key": key,
        **extra,
    }


def test_the_cookie_session_creates_replays_and_then_selects_the_organization(world):
    client = world["client"]
    created = client.post(URL, json={"name": "Cookie Org"}, headers=_headers(world))
    assert created.status_code == 201, created.text
    assert created.headers["cache-control"] == "no-store"
    assert "access_token" not in created.text
    replay = client.post(URL, json={"name": "Cookie Org"}, headers=_headers(world))
    assert replay.status_code == 200 and replay.json() == created.json()
    org_id = created.json()["organization"]["id"]
    listed = client.get("/api/v0/agents", headers={"X-Organization-Id": org_id})
    assert listed.status_code == 200
    changed = client.post(URL, json={"name": "Different"}, headers=_headers(world))
    assert changed.status_code == 409
    assert world["runtime"].api_credentials.list() == ()


def test_csrf_and_origin_are_enforced_and_nothing_is_created_without_them(world):
    client = world["client"]
    body = {"name": "Blocked"}
    assert (
        client.post(URL, json=body, headers={"Idempotency-Key": "k"}).status_code == 403
    )
    no_csrf = {"Origin": ORIGIN, "Idempotency-Key": "k"}
    assert client.post(URL, json=body, headers=no_csrf).status_code == 403
    bad = _headers(world, **{"X-Weaves-CSRF": "0" * 64})
    assert client.post(URL, json=body, headers=bad).status_code == 403
    cross = _headers(world, Origin="https://evil.test")
    assert client.post(URL, json=body, headers=cross).status_code == 403
    assert world["runtime"].organizations.list() == ()


def test_the_cookie_route_refuses_a_bearer_header_and_a_missing_cookie(world):
    client = world["client"]
    token = client.cookies.get(ACCOUNT_COOKIE_NAME)
    mixed = _headers(world, Authorization=f"Bearer {token}")
    assert client.post(URL, json={"name": "X"}, headers=mixed).status_code == 401
    client.cookies.clear()
    assert (
        client.post(URL, json={"name": "X"}, headers=_headers(world)).status_code == 401
    )
    assert world["runtime"].organizations.list() == ()
