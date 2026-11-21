"""Authenticated routes that attach or detach verified emails on an account."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from contracts.v1 import Account
from modules.auth.account_email_linking import (
    AccountEmailLinking,
    EmailAlreadyAttached,
    LastVerifiedIdentity,
)
from modules.auth.account_overview import list_identities, list_memberships
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.http.account_routes import (
    AccountEmailCodeRequest,
    AccountEmailVerifyRequest,
    throttled_response,
)
from shared.errors import EmailDeliveryUnavailable, LoginThrottled
from shared.request_context import system_scoped


def _current_account(request: Request) -> Account:
    """Return the account the middleware authenticated for this request."""
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="Account session required")
    return account


def _register_code(app: FastAPI, linking: AccountEmailLinking) -> None:
    """Register the request for an add-email proof code."""

    @app.post("/api/v0/account/identities/email-code", status_code=202)
    @system_scoped("account")
    def request_identity_code(
        payload: AccountEmailCodeRequest, request: Request
    ) -> dict[str, str]:
        account = _current_account(request)
        try:
            challenge_id = linking.start(
                account, payload.email, request.client.host if request.client else None
            )
        except LoginThrottled as exc:
            raise throttled_response(exc) from exc
        except EmailDeliveryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "message": "If the address can receive email, a code was sent.",
            "challenge_id": challenge_id,
        }


def _register_verify(
    app: FastAPI, linking: AccountEmailLinking, service: AccountSignInService
) -> None:
    """Register confirmation of the proof code."""

    @app.post("/api/v0/account/identities/email-verify")
    @system_scoped("account")
    def verify_identity_code(
        payload: AccountEmailVerifyRequest, request: Request
    ) -> dict[str, Any]:
        account = _current_account(request)
        try:
            linking.verify(account, payload.challenge_id, payload.code)
        except LoginThrottled as exc:
            raise throttled_response(exc) from exc
        except PermissionError as exc:
            # 400, not 401: the account session itself is still valid.
            raise HTTPException(
                status_code=400, detail="Email code is invalid or expired"
            ) from exc
        except EmailAlreadyAttached as exc:
            raise HTTPException(
                status_code=409,
                detail="This email address already belongs to another account",
            ) from exc
        return {
            "identities": list_identities(account, service.identities),
            "memberships": list_memberships(
                account, service.memberships, service.organizations
            ),
        }


def _register_remove(app: FastAPI, linking: AccountEmailLinking) -> None:
    """Register removal of one email identity."""

    @app.delete("/api/v0/account/identities/{identity_id}", status_code=204)
    @system_scoped("account")
    def remove_identity(identity_id: str, request: Request) -> Response:
        account = _current_account(request)
        try:
            linking.remove(account, identity_id)
        except PermissionError as exc:
            raise HTTPException(status_code=404, detail="Identity not found") from exc
        except LastVerifiedIdentity as exc:
            raise HTTPException(
                status_code=409,
                detail="An account must keep at least one verified email",
            ) from exc
        return Response(status_code=204)


def register_account_identity_routes(
    app: FastAPI, service: AccountSignInService
) -> None:
    """Register add-email proof, confirmation and removal for the signed-in account."""
    linking = AccountEmailLinking(service)
    _register_code(app, linking)
    _register_verify(app, linking, service)
    _register_remove(app, linking)
