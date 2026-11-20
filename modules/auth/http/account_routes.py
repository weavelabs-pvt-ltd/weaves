"""Account-level sign-in, session and membership discovery routes."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from contracts.v1 import Account
from modules.auth.account_overview import (
    account_summary,
    list_identities,
    list_memberships,
)
from modules.auth.account_sign_in import AccountSignInService
from shared.errors import EmailDeliveryUnavailable, LoginThrottled
from shared.request_context import system_scoped


class AccountEmailCodeRequest(BaseModel):
    """Request a sign-in code for any email address."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)


class AccountEmailVerifyRequest(BaseModel):
    """Redeem one code for an account session."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=16)


def _throttled(exc: LoginThrottled) -> HTTPException:
    """Map throttling to 429 with the standard Retry-After header."""
    return HTTPException(
        status_code=429,
        detail="Too many sign-in attempts. Try again later.",
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def _bearer(authorization: str | None) -> str:
    """Extract the bearer token the middleware has already authenticated."""
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer":
        raise HTTPException(status_code=401, detail="A bearer session is required")
    return token


def _register_sign_in(app: FastAPI, service: AccountSignInService) -> None:
    """Register the public code request and verification routes."""

    @app.post("/api/v0/account/email-code", status_code=202)
    @system_scoped("account")
    def request_account_email_code(
        payload: AccountEmailCodeRequest, request: Request
    ) -> dict[str, str]:
        try:
            challenge_id = service.start_email_login(
                payload.email, request.client.host if request.client else None
            )
        except LoginThrottled as exc:
            raise _throttled(exc) from exc
        except EmailDeliveryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "message": "If the address can receive email, a sign-in code was sent.",
            "challenge_id": challenge_id,
        }

    @app.post("/api/v0/account/email-verify")
    @system_scoped("account")
    def verify_account_email_code(
        payload: AccountEmailVerifyRequest, request: Request
    ) -> dict[str, Any]:
        try:
            account, session, token = service.verify_email_login(
                payload.challenge_id,
                payload.code,
                request.client.host if request.client else None,
            )
        except LoginThrottled as exc:
            raise _throttled(exc) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=401,
                detail="Email code is invalid, expired, or sign-in is not allowed",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
            "account": account_summary(account),
            "memberships": list_memberships(
                account, service.memberships, service.organizations
            ),
        }


def _register_session(app: FastAPI, service: AccountSignInService) -> None:
    """Register the authenticated account routes."""

    def current_account(request: Request) -> Account:
        account = getattr(request.state, "account", None)
        if not isinstance(account, Account):
            raise HTTPException(status_code=401, detail="Account session required")
        return account

    @app.get("/api/v0/account/me")
    @system_scoped("account")
    def get_account(request: Request) -> dict[str, Any]:
        account = current_account(request)
        return {
            "account": account_summary(account),
            "identities": list_identities(account, service.identities),
            "memberships": list_memberships(
                account, service.memberships, service.organizations
            ),
        }

    @app.post("/api/v0/account/logout", status_code=204)
    @system_scoped("account")
    def logout_account(
        request: Request, authorization: str | None = Header(default=None)
    ) -> Response:
        current_account(request)
        try:
            service.sessions.revoke(_bearer(authorization))
        except PermissionError as exc:
            raise HTTPException(
                status_code=401, detail="Account session required"
            ) from exc
        return Response(status_code=204)


def register_account_routes(app: FastAPI, service: AccountSignInService) -> None:
    """Register account-level routes; none grants tenant resource access."""
    _register_sign_in(app, service)
    _register_session(app, service)
