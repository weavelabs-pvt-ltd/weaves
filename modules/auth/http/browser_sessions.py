"""Cookie and CSRF helpers for same-origin browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import os
from urllib.parse import urlsplit

from fastapi import Response

from contracts.v1 import Principal
from modules.auth.authentication_service import AuthenticationService

COOKIE_NAME = "__Host-weaves_session"
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


def issue_browser_session(
    authentication: AuthenticationService, principal: Principal, response: Response
) -> None:
    """Create an HttpOnly cookie session with the normal revocation lifecycle."""
    _csrf_key()
    origin = browser_origin()
    if origin.startswith("http://") and not origin.startswith("http://localhost:"):
        raise RuntimeError("browser sessions require HTTPS outside localhost")
    session, token = authentication.create_user_session(principal, channel="browser")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(0, int((session.expires_at - session.created_at).total_seconds())),
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
