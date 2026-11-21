"""Cookie-backed account sessions for the same-origin browser app."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from contracts.v1 import Account
from modules.auth.account_overview import account_summary, list_memberships
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.http.account_routes import (
    AccountEmailVerifyRequest,
    throttled_response,
)
from modules.auth.http.browser_sessions import (
    ACCOUNT_COOKIE_NAME,
    clear_session_cookie,
    csrf_token,
    request_refusal,
    set_session_cookie,
)
from shared.errors import LoginThrottled
from shared.request_context import system_scoped


def _current_account(request: Request) -> Account:
    """Return the account the middleware authenticated from the cookie."""
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="Account session required")
    return account


def _rotate_out(service: AccountSignInService, request: Request) -> None:
    """Revoke the browser's previous account session, if it still holds one."""
    previous = request.cookies.get(ACCOUNT_COOKIE_NAME, "")
    if previous:
        with suppress(PermissionError):  # expired, revoked or foreign: skip
            service.sessions.revoke(previous, "browser")


def register_account_browser_routes(
    app: FastAPI, service: AccountSignInService
) -> None:
    """Register the account cookie routes; the middleware guards all but verify."""

    @app.post("/api/v0/account/browser/email-verify")
    @system_scoped("account")
    def verify_account_browser_email(
        payload: AccountEmailVerifyRequest, request: Request, response: Response
    ) -> Any:
        refusal = request_refusal(request, None)
        if refusal is not None:
            return refusal
        try:
            account, session, token = service.verify_email_login(
                payload.challenge_id,
                payload.code,
                request.client.host if request.client else None,
                channel="browser",
            )
        except LoginThrottled as exc:
            raise throttled_response(exc) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=401,
                detail="Email code is invalid, expired, or sign-in is not allowed",
            ) from exc
        _rotate_out(service, request)
        set_session_cookie(
            response,
            ACCOUNT_COOKIE_NAME,
            token,
            max_age=int((session.expires_at - session.created_at).total_seconds()),
        )
        return {
            "expires_at": session.expires_at,
            "csrf_token": csrf_token(token),
            "account": account_summary(account),
            "memberships": list_memberships(
                account, service.memberships, service.organizations
            ),
        }

    @app.get("/api/v0/account/browser/me")
    @system_scoped("account")
    def get_account_browser_me(request: Request, response: Response) -> dict[str, Any]:
        account = _current_account(request)
        response.headers["Cache-Control"] = "no-store"
        return {
            "account": account_summary(account),
            "memberships": list_memberships(
                account, service.memberships, service.organizations
            ),
            "csrf_token": csrf_token(request.cookies[ACCOUNT_COOKIE_NAME]),
        }

    @app.post("/api/v0/account/browser/logout", status_code=204)
    @system_scoped("account")
    def logout_account_browser(request: Request) -> Response:
        _current_account(request)
        try:
            service.sessions.revoke(request.cookies[ACCOUNT_COOKIE_NAME], "browser")
        except PermissionError as exc:
            raise HTTPException(
                status_code=401, detail="Account session required"
            ) from exc
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        clear_session_cookie(response, ACCOUNT_COOKIE_NAME)
        return response
