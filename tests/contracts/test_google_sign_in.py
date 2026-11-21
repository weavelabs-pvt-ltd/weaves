"""Google OIDC sign-in: start, state binding, token verification, failures."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.auth.http.browser_sessions import ACCOUNT_COOKIE_NAME
from shared.clock import utc_now
from tests.support.account_session import (
    CALLBACK,
    ORIGIN,
    START,
    google_sign_in,
)
from tests.support.fake_google import (
    AUTHORIZE,
    CLIENT_ID,
    CLIENT_SECRET,
    DISCOVERY,
    ISSUER,
    JWKS,
)
from tests.support.google_world import BINDING_COOKIE, EMAIL, cookies_of, google_world

__all__ = ["google_world"]

ERROR = "/#account-signin?error="


def _start(world, **params):
    return world.client.get(START, params=params, follow_redirects=False)


def _callback(world, query, **kwargs):
    return world.client.get(CALLBACK, params=query, follow_redirects=False, **kwargs)


def _rows(world):
    """Counts that must not change when a sign-in is refused."""
    runtime = world.runtime
    return (
        len(runtime.accounts.list()),
        len(runtime.account_identities.list()),
        len(runtime.account_sessions.list()),
    )


def _refused(world, response, code):
    assert response.status_code == 302, response.text
    assert response.headers["location"] == ERROR + code
    assert ACCOUNT_COOKIE_NAME not in cookies_of(response)


def test_start_redirects_to_google_with_pkce_state_nonce_and_a_bound_cookie(
    google_world,
):
    world = google_world()
    response = _start(world, return_to="/studio?x=1")
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(AUTHORIZE + "?")
    query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == ORIGIN + "/api/v0/account/google/callback"
    assert query["response_type"] == "code" and query["scope"] == "openid email profile"
    assert (
        query["code_challenge_method"] == "S256" and query["prompt"] == "select_account"
    )
    assert len(query["state"]) >= 40 and len(query["nonce"]) >= 24
    assert CLIENT_SECRET not in location
    morsel = cookies_of(response)[BINDING_COOKIE]
    assert morsel["httponly"] and morsel["secure"] and morsel["samesite"] == "lax"
    assert morsel["max-age"] == "600" and morsel["path"] == "/"
    assert "domain" not in morsel.output().lower()
    assert response.headers["cache-control"] == "no-store"
    (record,) = world.runtime.account_oauth_states.list()
    # Only digests of the secrets are stored; the verifier never leaves the server.
    dump = record.model_dump_json()
    assert query["state"] not in dump and morsel.value not in dump
    assert record.return_to == "/studio?x=1" and record.nonce == query["nonce"]
    assert record.expires_at - record.created_at == timedelta(minutes=10)
    assert record.code_verifier not in location


def test_callback_signs_in_sets_the_session_cookie_and_clears_the_binding(google_world):
    world = google_world()
    response = google_sign_in(world.client, world.fake, EMAIL, return_to="/agents")
    assert response.status_code == 302 and response.headers["location"] == "/agents"
    cookies = cookies_of(response)
    session = cookies[ACCOUNT_COOKIE_NAME]
    assert session["httponly"] and session["secure"] and session["samesite"] == "strict"
    assert session.value.startswith("wvs_acbr_") and session["max-age"] == "43200"
    assert cookies[BINDING_COOKIE]["max-age"] == "0"  # cleared
    assert response.headers["cache-control"] == "no-store"
    assert world.runtime.account_oauth_states.list() == ()  # consumed
    assert world.client.get("/api/v0/account/browser/me").status_code == 200


def test_signing_in_again_rotates_the_browser_session(google_world):
    world = google_world()
    google_sign_in(world.client, world.fake, EMAIL)
    first = world.client.cookies.get(ACCOUNT_COOKIE_NAME)
    google_sign_in(world.client, world.fake, EMAIL)
    second = world.client.cookies.get(ACCOUNT_COOKIE_NAME)
    assert first != second
    assert world.client.get("/api/v0/account/browser/me").status_code == 200
    world.client.cookies.set(ACCOUNT_COOKIE_NAME, first)
    assert world.client.get("/api/v0/account/browser/me").status_code == 401
    revoked = [s for s in world.runtime.account_sessions.list() if s.revoked_at]
    assert len(revoked) == 1


@pytest.mark.parametrize(
    "value",
    [
        "//evil.test/x",
        "https://evil.test/",
        "http:evil.test",
        "/\\evil.test",
        "\\\\evil.test",
        "evil.test/path",
        "/ok\nSet-Cookie: x=1",
        "/with space",
        "/tab\there",
        "/" + "a" * 600,
    ],
)
def test_start_refuses_a_return_to_that_is_not_a_same_origin_path(google_world, value):
    world = google_world()
    response = _start(world, return_to=value)
    assert response.status_code == 400
    assert world.runtime.account_oauth_states.list() == ()
    assert BINDING_COOKIE not in cookies_of(response)


def test_the_default_return_to_is_the_app_root(google_world):
    world = google_world()
    response = google_sign_in(world.client, world.fake, EMAIL)
    assert response.headers["location"] == "/"


def test_state_replay_is_refused_and_consumed_exactly_once(google_world):
    world = google_world()
    start = _start(world)
    query = world.fake.authorize(start.headers["location"], email=EMAIL)
    binding = world.client.cookies.get(BINDING_COOKIE)
    ok = _callback(world, query)
    assert ok.headers["location"] == "/"
    before = _rows(world)
    world.client.cookies.clear()
    world.client.cookies.set(BINDING_COOKIE, binding)  # replay, right cookie
    replay = _callback(world, query)
    _refused(world, replay, "state")
    assert _rows(world) == before


def test_a_state_that_was_never_issued_is_refused(google_world):
    world = google_world()
    _start(world)
    _refused(world, _callback(world, {"code": "c", "state": "forged"}), "state")
    _refused(world, _callback(world, {"code": "c"}), "state")
    _refused(world, _callback(world, {}), "state")
    assert world.runtime.accounts.list() == ()


def test_an_expired_state_is_refused_and_removed(google_world):
    world = google_world()
    start = _start(world)
    query = world.fake.authorize(start.headers["location"], email=EMAIL)
    (record,) = world.runtime.account_oauth_states.list()
    world.runtime.account_oauth_states.put(
        record.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    _refused(world, _callback(world, query), "state")
    assert world.runtime.account_oauth_states.list() == ()
    assert world.runtime.accounts.list() == ()


def test_a_missing_or_wrong_cookie_binding_is_refused_and_burns_the_state(
    google_world,
):
    world = google_world()
    for tamper in ("missing", "wrong"):
        start = _start(world)
        query = world.fake.authorize(start.headers["location"], email=EMAIL)
        if tamper == "missing":
            world.client.cookies.delete(BINDING_COOKIE)
        else:
            world.client.cookies.set(BINDING_COOKIE, "attacker-chosen-value")
        _refused(world, _callback(world, query), "state")
        assert world.runtime.account_oauth_states.list() == ()
    assert world.runtime.accounts.list() == ()


def test_a_binding_cookie_from_another_attempt_is_refused(google_world):
    world = google_world()
    first = _start(world)
    first_cookie = world.client.cookies.get(BINDING_COOKIE)
    second = _start(world)  # the browser now holds the second attempt's cookie
    query = world.fake.authorize(first.headers["location"], email=EMAIL)
    assert first_cookie != world.client.cookies.get(BINDING_COOKIE)
    _refused(world, _callback(world, query), "state")
    assert second.status_code == 302 and world.runtime.accounts.list() == ()


def test_google_reporting_an_error_ends_in_a_fixed_code(google_world):
    world = google_world()
    start = _start(world)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    denied = _callback(world, {"error": "access_denied", "state": state})
    _refused(world, denied, "access_denied")
    start = _start(world)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    odd = _callback(world, {"error": "server_error<script>", "state": state})
    _refused(world, odd, "unavailable")
    assert "script" not in odd.headers["location"]


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"email_verified": False}, "unverified email"),
        ({"email_verified": None}, "missing email_verified"),
        ({"email": None}, "missing email"),
        ({"aud": "someone-else"}, "wrong audience"),
        ({"iss": "https://evil.test"}, "wrong issuer"),
        ({"nonce": "not-the-nonce"}, "wrong nonce"),
        ({"nonce": None}, "missing nonce"),
        ({"sub": None}, "missing subject"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"iat": int(time.time()) + 3600}, "issued in the future"),
        ({"sign_with": 7}, "bad signature"),
    ],
)
def test_an_id_token_that_fails_verification_creates_nothing(
    google_world, claims, reason
):
    world = google_world()
    response = google_sign_in(world.client, world.fake, **{"email": EMAIL, **claims})
    _refused(world, response, "access_denied")
    assert _rows(world) == (0, 0, 0), reason


def test_both_google_issuer_spellings_are_accepted(google_world):
    world = google_world()
    bare = google_sign_in(
        world.client, world.fake, EMAIL, iss="accounts.google.com", sub="s-bare"
    )
    assert bare.headers["location"] == "/"
    full = google_sign_in(world.client, world.fake, EMAIL, iss=ISSUER, sub="s-bare")
    assert full.headers["location"] == "/"
    assert len(world.runtime.accounts.list()) == 1  # canonical issuer stored once


def test_clock_skew_within_a_minute_is_tolerated(google_world):
    world = google_world()
    soon = google_sign_in(world.client, world.fake, EMAIL, iat=int(time.time()) + 30)
    assert soon.headers["location"] == "/"
    world.client.cookies.clear()
    just_expired = google_sign_in(
        world.client, world.fake, EMAIL, exp=int(time.time()) - 30
    )
    assert just_expired.headers["location"] == "/"


def test_an_unknown_kid_refreshes_the_jwks_once_and_accepts_the_rotated_key(
    google_world,
):
    world = google_world()
    clock = {"now": 1000.0}
    world.google.provider._monotonic = lambda: clock["now"]
    assert google_sign_in(world.client, world.fake, EMAIL).headers["location"] == "/"
    fetches = world.fake.calls.count(f"GET {JWKS}")
    google_sign_in(world.client, world.fake, EMAIL)
    assert world.fake.calls.count(f"GET {JWKS}") == fetches  # cached
    world.fake.rotate()
    clock["now"] += 10  # past the refresh cooldown
    rotated = google_sign_in(world.client, world.fake, EMAIL)
    assert rotated.headers["location"] == "/"
    assert world.fake.calls.count(f"GET {JWKS}") == fetches + 1


def test_a_forged_kid_cannot_force_repeated_jwks_fetches(google_world):
    world = google_world()
    clock = {"now": 1000.0}
    world.google.provider._monotonic = lambda: clock["now"]
    google_sign_in(world.client, world.fake, EMAIL)
    fetches = world.fake.calls.count(f"GET {JWKS}")
    for _ in range(3):
        refused = google_sign_in(world.client, world.fake, EMAIL, kid="forged")
        _refused(world, refused, "access_denied")
    assert world.fake.calls.count(f"GET {JWKS}") == fetches  # inside the cooldown


def test_a_token_signed_by_a_retired_key_is_refused_after_rotation(google_world):
    world = google_world()
    clock = {"now": 1000.0}
    world.google.provider._monotonic = lambda: clock["now"]
    google_sign_in(world.client, world.fake, EMAIL)
    world.fake.rotate()
    clock["now"] += 4000  # JWKS cache expired too
    stale = google_sign_in(world.client, world.fake, EMAIL, sign_with=0, kid="kid-0")
    _refused(world, stale, "access_denied")


def test_a_code_exchange_failure_is_a_fixed_error_and_creates_nothing(google_world):
    world = google_world()
    world.fake.token_status = 400
    _refused(world, google_sign_in(world.client, world.fake, EMAIL), "access_denied")
    world.fake.token_status = 503
    _refused(world, google_sign_in(world.client, world.fake, EMAIL), "unavailable")
    assert _rows(world) == (0, 0, 0)


def test_the_code_is_bound_to_the_pkce_verifier_and_secret(google_world):
    world = google_world()
    start = _start(world)
    query = world.fake.authorize(start.headers["location"], email=EMAIL)
    world.fake.codes[query["code"]]["challenge"] = "a-different-challenge"
    _refused(world, _callback(world, query), "access_denied")


def test_an_unreachable_provider_is_unavailable(google_world):
    world = google_world()
    world.fake.discovery_status = 500
    assert _start(world).status_code == 503
    assert world.runtime.account_oauth_states.list() == ()


def test_discovery_is_fetched_once_and_cached(google_world):
    world = google_world()
    google_sign_in(world.client, world.fake, EMAIL)
    google_sign_in(world.client, world.fake, EMAIL)
    assert world.fake.calls.count(f"GET {DISCOVERY}") == 1


def test_failures_never_leak_secrets_or_provider_details(google_world, caplog):
    world = google_world()
    caplog.set_level(logging.DEBUG)
    world.fake.token_status = 400
    refused = google_sign_in(world.client, world.fake, EMAIL)
    google_sign_in(world.client, world.fake, EMAIL, aud="x")
    world.fake.token_status = 200
    ok = google_sign_in(world.client, world.fake, EMAIL)
    token = world.client.cookies.get(ACCOUNT_COOKIE_NAME)
    ours = " ".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("product")
    )
    text = ours + refused.text + refused.headers["location"]
    for secret in (CLIENT_SECRET, token, "invalid_grant"):
        assert secret not in text
    assert "account google sign-in refused" in ours
    assert "account google sign-in ok" in ours
    assert ok.status_code == 302


def test_the_routes_are_not_found_while_google_is_not_configured(google_world):
    world = google_world(configured=False)
    assert world.client.get(START).status_code == 404
    assert (
        world.client.get(CALLBACK, params={"code": "c", "state": "s"}).status_code
        == 404
    )
    assert world.google is None


def test_a_client_id_without_a_secret_stays_unconfigured(google_world, monkeypatch):
    monkeypatch.setenv("WEAVES_GOOGLE_CLIENT_ID", CLIENT_ID)
    world = google_world(configured=False)
    assert world.client.get(START).status_code == 404


def test_start_is_unavailable_without_a_browser_session_secret(
    google_world, monkeypatch
):
    world = google_world()
    monkeypatch.setenv("WEAVES_BROWSER_SESSION_SECRET", "short")
    assert _start(world).status_code == 503
    assert world.runtime.account_oauth_states.list() == ()


def test_both_routes_are_public_and_never_take_a_bearer_or_cookie(google_world):
    world = google_world()
    # No Authorization is needed, and a junk one does not make start fail.
    junk = world.client.get(
        START, headers={"Authorization": "Bearer junk"}, follow_redirects=False
    )
    assert junk.status_code == 302
    openapi = world.client.get("/openapi.json").json()["paths"]
    assert "/api/v0/account/google/start" in openapi
    assert "/api/v0/account/google/callback" in openapi
