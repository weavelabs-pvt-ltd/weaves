"""Cookie-backed account session and profile routes for the same-origin app."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from contracts.v1 import Account
from modules.auth.account_organizations import AccountOrganizationService
from modules.auth.account_overview import account_summary
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.http.account_routes import (
    AccountPatchRequest,
    account_view,
    update_profile,
)
from modules.auth.http.browser_sessions import (
    ACCOUNT_COOKIE_NAME,
    clear_session_cookie,
    csrf_token,
)
from shared.request_context import system_scoped


def _current_account(request: Request) -> Account:
    """Return the account the middleware authenticated from the cookie."""
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="Account session required")
    return account


def register_account_browser_routes(
    app: FastAPI,
    service: AccountSignInService,
    organizations: AccountOrganizationService,
) -> None:
    """Register the account cookie routes; the middleware guards every one."""

    @app.get("/api/v0/account/browser/me")
    @system_scoped("account")
    def get_account_browser_me(request: Request, response: Response) -> dict[str, Any]:
        account = _current_account(request)
        response.headers["Cache-Control"] = "no-store"
        return {
            **account_view(account, service, organizations),
            "csrf_token": csrf_token(request.cookies[ACCOUNT_COOKIE_NAME]),
        }

    @app.patch("/api/v0/account/browser/me")
    @system_scoped("account")
    def patch_account_browser_me(
        payload: AccountPatchRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        account = update_profile(service, _current_account(request), payload)
        response.headers["Cache-Control"] = "no-store"
        return {"account": account_summary(account)}

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
