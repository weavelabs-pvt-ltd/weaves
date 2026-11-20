"""Cookie and CSRF helpers for same-origin browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import os
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.responses import JSONResponse

from contracts.v1 import Principal
from modules.auth.authentication_service import AuthenticationService

COOKIE_NAME = "__Host-weaves_session"
ACCOUNT_COOKIE_NAME = "__Host-weaves_account_session"
CSRF_HEADER = "x-weaves-csrf"


def browser_origin() -> str:
    """Return the single configured app origin, rejecting ambiguous URLs."""
    url = urlsplit(os.getenv("WEAVES_PUBLIC_FRONTEND_URL", "http://localhost:8080"))
    if url.scheme not in {"http", "https"} or not url.hostname or url.username:
        raise RuntimeError("WEAVES_PUBLIC_FRONTEND_URL must be an app origin")
    if url.path not in {"", "/"} or url.query or url.fragment:
        raise RuntimeError("WEAVES_PUBLIC_FRONTEND_URL must be an app origin")
    return f"{url.scheme}://{url.netloc}"


def _csrf_key() -> bytes:
    """Require a dedicated deployment secret for browser CSRF tokens."""
    secret = os.getenv("WEAVES_BROWSER_SESSION_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("WEAVES_BROWSER_SESSION_SECRET must be at least 32 bytes")
    return secret.encode("utf-8")


def browser_sessions_configured() -> bool:
    """Report whether cookie and CSRF operation has a sufficient secret."""
    return len(os.getenv("WEAVES_BROWSER_SESSION_SECRET", "").encode("utf-8")) >= 32


def csrf_token(session_token: str) -> str:
    """Derive a session-bound token without exposing the session credential."""
    digest = hashlib.sha256(session_token.encode("utf-8")).digest()
    return hmac.new(
        _csrf_key(), b"weaves-browser-csrf-v1:" + digest, "sha256"
    ).hexdigest()


def csrf_valid(session_token: str, supplied: str) -> bool:
    """Compare the submitted token in constant time."""
    if not supplied.isascii() or len(supplied) != 64:
        return False
    return hmac.compare_digest(csrf_token(session_token), supplied)


def request_refusal(
    request: Request, csrf_session_token: str | None
) -> JSONResponse | None:
    """Refuse cross-origin or unprotected cookie traffic, else return None.

    Passing the session token also demands the CSRF header on unsafe methods;
    passing None (a public sign-in call that has no session yet) still demands
    an allowed Origin on unsafe methods.
    """
    expected_origin = browser_origin()
    if (
        not browser_sessions_configured()
        or request.headers.get("host", "") != urlsplit(expected_origin).netloc
        or request.url.scheme != urlsplit(expected_origin).scheme
    ):
        return JSONResponse(
            content={"detail": "Browser session unavailable"}, status_code=401
        )
    if request.headers.get(
        "origin", expected_origin
    ) != expected_origin or request.headers.get(
        "sec-fetch-site", "same-origin"
    ) not in {"same-origin", "none"}:
        return JSONResponse(
            content={"detail": "Cross-origin request rejected"}, status_code=403
        )
    if request.method not in {"GET", "HEAD"} and (
        request.headers.get("origin") != expected_origin
        or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
        or (
            csrf_session_token is not None
            and not csrf_valid(csrf_session_token, request.headers.get(CSRF_HEADER, ""))
        )
    ):
        return JSONResponse(content={"detail": "CSRF check failed"}, status_code=403)
    return None


def set_session_cookie(
    response: Response, name: str, token: str, *, max_age: int
) -> None:
    """Attach a Secure, HttpOnly, SameSite=Strict session cookie."""
    _csrf_key()
    origin = browser_origin()
    if origin.startswith("http://") and not origin.startswith("http://localhost:"):
        raise RuntimeError("browser sessions require HTTPS outside localhost")
    response.set_cookie(
        name,
        token,
        max_age=max(0, max_age),
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookie(response: Response, name: str) -> None:
    """Expire a session cookie with the attributes it was set with."""
    response.delete_cookie(
        name, path="/", secure=True, httponly=True, samesite="strict"
    )


def issue_browser_session(
    authentication: AuthenticationService, principal: Principal, response: Response
) -> None:
    """Create an HttpOnly cookie session with the normal revocation lifecycle."""
    _csrf_key()
    session, token = authentication.create_user_session(principal, channel="browser")
    set_session_cookie(
        response,
        COOKIE_NAME,
        token,
        max_age=int((session.expires_at - session.created_at).total_seconds()),
    )
