"""Sign people in for tests: through the fake Google provider, or by bearer token.

``google_*`` helpers drive the real start and callback routes against a
``FakeGoogle`` and return cookie, CSRF and organization headers. ``sign_in`` and
``account_headers`` skip the browser and issue a bearer session directly with
``AccountSessions.issue`` for tests that only need a token.
"""

from __future__ import annotations

from typing import Any

import httpx

from tests.support.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle

ORIGIN = "https://app.example.test"
START = "/api/v0/account/google/start"
CALLBACK = "/api/v0/account/google/callback"


def configure_google(monkeypatch: Any, **env: str) -> None:
    """Configure Google sign-in and browser sessions for the next app start."""
    monkeypatch.setenv("WEAVES_PUBLIC_FRONTEND_URL", ORIGIN)
    monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "browser-secret-" * 4)
    monkeypatch.setenv("WEAVES_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("WEAVES_GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    for name in (
        "WEAVES_ACCOUNT_ALLOWED_EMAILS",
        "WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS",
        "WEAVES_GOOGLE_DISCOVERY_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def google_sign_in(
    client: Any,
    fake: FakeGoogle,
    email: str = "person@example.test",
    *,
    return_to: str | None = None,
    **claims: Any,
) -> httpx.Response:
    """Run start, the fake consent screen and the callback; return the callback."""
    params = {"return_to": return_to} if return_to is not None else {}
    start = client.get(START, params=params, follow_redirects=False)
    assert start.status_code == 302, start.text
    query = fake.authorize(start.headers["location"], email=email, **claims)
    return client.get(CALLBACK, params=query, follow_redirects=False)


def google_headers(
    client: Any,
    fake: FakeGoogle,
    email: str,
    organization_id: str | None = None,
    workspace_id: str | None = None,
    **claims: Any,
) -> dict[str, str]:
    """Sign ``email`` in with Google and return cookie-session request headers.

    The cookie lives in the client's jar; the headers carry the same-origin
    ``Origin``, the ``X-Weaves-CSRF`` token and ``X-Organization-Id``.
    """
    callback = google_sign_in(client, fake, email, **claims)
    assert callback.status_code == 302, callback.text
    assert "error=" not in callback.headers["location"], callback.headers["location"]
    me = client.get("/api/v0/account/browser/me")
    assert me.status_code == 200, me.text
    headers = {"Origin": ORIGIN, "X-Weaves-CSRF": me.json()["csrf_token"]}
    if organization_id is not None:
        headers["X-Organization-Id"] = organization_id
    if workspace_id is not None:
        headers["X-Workspace-ID"] = workspace_id
    return headers


def sign_in(service: Any, email: str) -> tuple[Any, str]:
    """Issue a bearer session for an email directly; return (account, token).

    The account is resolved or created from the verified email, its
    memberships are linked exactly as at sign-in, and ``AccountSessions.issue``
    mints the token. No browser, provider or mailbox is involved.
    """
    normalized = email.strip().casefold()
    account = service.find_or_create_account(normalized)
    service.linker.link(account, normalized)
    _, token = service.sessions.issue(account, "bearer")
    return account, token


def account_headers(
    client: Any,
    email: str,
    organization_id: str,
    workspace_id: str | None = None,
) -> dict[str, str]:
    """Issue a bearer session for ``email`` and return tenant request headers.

    ``client`` is a started TestClient. The returned headers carry the account
    bearer session and ``X-Organization-Id`` (plus ``X-Workspace-ID`` if given).
    """
    service = client.app.state.authentication_services.accounts
    _, token = sign_in(service, email)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": organization_id,
    }
    if workspace_id is not None:
        headers["X-Workspace-ID"] = workspace_id
    return headers
