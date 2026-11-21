"""Fixture client: real Google sign-in over HTTP, then the public resource APIs."""

import hashlib
import os
import time
from urllib.parse import parse_qsl, urlsplit

import httpx

API = "http://api:8000/api/v0"
PROVIDER = "https://provider"
GOOGLE = "http://provider:8081/google"
SESSION_COOKIE = "__Host-weaves_account_session"


def app_origin():
    """The one origin the API accepts for cookie sessions (the harness proxy)."""
    return os.environ["WEAVES_PUBLIC_FRONTEND_URL"]


def request(method, path, headers=None, body=None, **kwargs):
    """Send a request as the browser's same-origin proxy would, without redirects."""
    sent = {"Host": urlsplit(app_origin()).netloc, **(headers or {})}
    kwargs.setdefault("follow_redirects", False)
    return httpx.request(
        method, API + path, headers=sent, json=body, timeout=30, **kwargs
    )


def call(method, path, actor=None, body=None, expected=(200, 201, 202, 204)):
    """Call the actual API without logging tokens, cookies, or credential bodies."""
    response = request(method, path, actor["headers"] if actor else {}, body)
    if response.status_code not in expected:
        raise AssertionError(f"Fixture {method} {path}: HTTP {response.status_code}")
    return response.json() if response.status_code != 204 else None


def cookie_value(response, name):
    """Read one Set-Cookie value without keeping a jar (Secure cookies, plain HTTP)."""
    for header in response.headers.get_list("set-cookie"):
        pair = header.split(";", 1)[0]
        if pair.startswith(name + "="):
            return pair.split("=", 1)[1]
    return ""


def google_sign_in(email, *, verified=True, deny=False, return_to="/"):
    """Drive the Google redirect flow as a browser would; return (location, token).

    The authorize hop goes to the fake provider's internal address, because the
    host-published proxy address is not reachable from inside the Docker network;
    the browser controls exercise the public proxy path.
    """
    started = request("GET", "/account/google/start", params={"return_to": return_to})
    if started.status_code != 302:
        raise AssertionError(f"Google start: HTTP {started.status_code}")
    binding = cookie_value(started, "__Host-weaves_google_oauth")
    target = urlsplit(started.headers["location"])
    if target.path != "/fake-google/authorize" or not binding:
        raise AssertionError("Google start did not redirect to the proxied authorize")
    query = dict(parse_qsl(target.query))
    query["login_hint"] = email
    if not verified:
        query["fake_unverified"] = "1"
    if deny:
        query["fake_deny"] = "1"
    approved = httpx.get(GOOGLE + "/authorize", params=query, follow_redirects=False)
    if approved.status_code != 302:
        raise AssertionError(f"Fake Google authorize: HTTP {approved.status_code}")
    callback = dict(parse_qsl(urlsplit(approved.headers["location"]).query))
    finished = request(
        "GET",
        "/account/google/callback",
        {"Cookie": "__Host-weaves_google_oauth=" + binding},
        params=callback,
    )
    return finished.headers.get("location", ""), cookie_value(finished, SESSION_COOKIE)


def source_for(email):
    """Give each synthetic person a stable client address so native throttles apply."""
    digest = hashlib.sha256(email.encode()).digest()
    return {"X-Forwarded-For": f"198.18.{digest[0]}.{digest[1] % 250 + 1}"}


def browser_headers(token, csrf):
    """The headers a signed-in browser sends: cookie, CSRF and same-origin Origin."""
    return {
        "Cookie": f"{SESSION_COOKIE}={token}",
        "X-Weaves-CSRF": csrf,
        "Origin": app_origin(),
    }


def account_session(email):
    """Sign an account in through Google and return its cookie session headers."""
    location, token = google_sign_in(email)
    if not token or location != "/":
        raise AssertionError(f"Google sign-in did not create a session: {location}")
    me = request(
        "GET",
        "/account/browser/me",
        {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": app_origin()},
    )
    if me.status_code != 200:
        raise AssertionError(f"Fixture session profile: HTTP {me.status_code}")
    return browser_headers(token, me.json()["csrf_token"])


def actor(session, org_id, workspace, principal, email):
    """Describe a cookie session acting as one tenant-local principal in an org."""
    return {
        "headers": {
            **session,
            "X-Organization-Id": org_id,
            "X-Workspace-ID": workspace,
        },
        "principal_id": principal,
        "email": email,
    }


def service_actor(token, workspace, principal):
    """A workspace-bound service account uses its API credential as a bearer."""
    return {
        "headers": {"Authorization": "Bearer " + token, "X-Workspace-ID": workspace},
        "principal_id": principal,
        "email": "service",
    }


def wait_for_services():
    """Wait for PostgreSQL-backed API and fake providers with a bounded timeout."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            health = httpx.get(API + "/health", timeout=3).json()
            fake = httpx.get(PROVIDER + "/health", timeout=3).json()
            google = httpx.get(GOOGLE + "/jwks", timeout=3).json()
            if (
                health == {"status": "ok", "storage": "postgres"}
                and fake.get("fixture") == "tenancy-phase-0"
                and google.get("keys")
            ):
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Disposable tenancy services failed to become ready")
