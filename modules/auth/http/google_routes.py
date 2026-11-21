"""Public Google sign-in routes: start the redirect, finish at the callback."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from modules.auth.google_account_resolution import SignInRefused
from modules.auth.google_oidc import ProviderUnavailable
from modules.auth.google_sign_in import GoogleSignIn, safe_return_to
from modules.auth.http.browser_sessions import (
    ACCOUNT_COOKIE_NAME,
    browser_sessions_configured,
    set_session_cookie,
)
from shared.request_context import system_scoped

logger = logging.getLogger("product.account")
BINDING_COOKIE_NAME = "__Host-weaves_google_oauth"
BINDING_COOKIE_SECONDS = 600
ERROR_URL = "/#account-signin?error="


def _failure(code: str) -> RedirectResponse:
    """Redirect to the app with one fixed, detail-free error code."""
    response = RedirectResponse(ERROR_URL + code, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    return response


def _clear_binding(response: Response) -> None:
    """Expire the oauth binding cookie with the attributes it was set with."""
    response.delete_cookie(
        BINDING_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax"
    )


def register_google_routes(app: FastAPI, google: GoogleSignIn | None) -> None:
    """Register both routes; they answer 404 while Google is not configured."""
    app.state.google_sign_in = google

    @app.get("/api/v0/account/google/start", include_in_schema=True)
    @system_scoped("account")
    def google_start(request: Request) -> Response:
        if google is None:
            raise HTTPException(status_code=404, detail="Not found")
        return_to = safe_return_to(request.query_params.get("return_to"))
        if return_to is None:
            raise HTTPException(status_code=400, detail="return_to must be a path")
        if not browser_sessions_configured():
            raise HTTPException(status_code=503, detail="Sign-in is unavailable")
        try:
            started = google.start(return_to)
        except ProviderUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="Sign-in is unavailable"
            ) from exc
        response = RedirectResponse(started.authorization_url, status_code=302)
        response.set_cookie(
            BINDING_COOKIE_NAME,
            started.binding,
            max_age=BINDING_COOKIE_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v0/account/google/callback", include_in_schema=True)
    @system_scoped("account")
    def google_callback(request: Request) -> Response:
        if google is None:
            raise HTTPException(status_code=404, detail="Not found")
        response = _complete(google, request)
        _clear_binding(response)
        return response


def _complete(google: GoogleSignIn, request: Request) -> Response:
    """Finish the sign-in; every failure becomes a fixed-code redirect."""
    query = request.query_params
    try:
        result = google.finish(
            code=query.get("code"),
            state=query.get("state"),
            binding=request.cookies.get(BINDING_COOKIE_NAME),
            provider_error=query.get("error"),
        )
        response = RedirectResponse(result.return_to, status_code=302)
        _rotate_out(google, request)
        set_session_cookie(
            response,
            ACCOUNT_COOKIE_NAME,
            result.token,
            max_age=int(
                (result.session.expires_at - result.session.created_at).total_seconds()
            ),
        )
    except SignInRefused as exc:
        logger.info("account google sign-in refused code=%s", exc.code)
        return _failure(exc.code)
    except Exception:
        logger.exception("account google sign-in failed code=unavailable")
        return _failure("unavailable")
    return response


def _rotate_out(google: GoogleSignIn, request: Request) -> None:
    """Revoke the account session this browser presented, if it still holds one."""
    previous: Any = request.cookies.get(ACCOUNT_COOKIE_NAME, "")
    if previous:
        with suppress(PermissionError):  # expired, revoked or foreign: skip
            google.service.sessions.revoke(previous, "browser")
