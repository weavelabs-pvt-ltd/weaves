"""Account-level session, profile and membership discovery routes (bearer)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.v1 import Account
from modules.auth.account_overview import (
    account_summary,
    list_identities,
    list_memberships,
)
from modules.auth.account_sign_in import AccountSignInService
from shared.clock import utc_now
from shared.errors import LoginThrottled
from shared.request_context import system_scoped


class AccountPatchRequest(BaseModel):
    """Editable account profile fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=160)

    @field_validator("display_name")
    @classmethod
    def printable(cls, value: str) -> str:
        """Allow only printable characters, with no control or line breaks."""
        if not value.isprintable():
            raise ValueError("display_name must be printable text")
        return value


def rename_account(
    service: AccountSignInService, account: Account, display_name: str
) -> Account:
    """Persist a new display name and return the updated account."""
    updated = Account.model_validate(
        account.model_copy(
            update={"display_name": display_name, "updated_at": utc_now()}
        ).model_dump()
    )
    service.accounts.put(updated)
    return updated


def throttled_response(exc: LoginThrottled) -> HTTPException:
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

    @app.patch("/api/v0/account/me")
    @system_scoped("account")
    def patch_account(payload: AccountPatchRequest, request: Request) -> dict[str, Any]:
        account = rename_account(
            service, current_account(request), payload.display_name
        )
        return {"account": account_summary(account)}

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
    _register_session(app, service)
