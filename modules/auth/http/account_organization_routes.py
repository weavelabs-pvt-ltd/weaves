"""Create an organization from an authenticated account session."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from contracts.v1 import Account
from modules.auth.account_organization_input import AccountOrganizationCommand
from modules.auth.account_organizations import (
    AccountOrganizationResult,
    AccountOrganizationService,
    IdempotencyConflict,
    OrganizationLimitReached,
    creation_enabled,
)
from modules.auth.http.account_routes import throttled_response
from shared.errors import LoginThrottled

IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


class AccountOrganizationRequest(BaseModel):
    """Organization name and owner preferences; the caller becomes its Admin."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    owner_email: str | None = Field(default=None, min_length=3, max_length=320)
    display_name: str | None = Field(default=None, max_length=160)
    timezone: str | None = Field(default=None, max_length=64)
    locale: str | None = Field(default=None, max_length=35)


def _view(result: AccountOrganizationResult) -> dict[str, Any]:
    """The tenant is returned without any bearer token or API credential."""
    organization, workspace, membership = (
        result.organization,
        result.workspace,
        result.membership,
    )
    return {
        "organization": {
            "id": organization.id,
            "name": organization.name,
            "status": organization.status.value,
        },
        "workspace": {"id": workspace.id, "name": workspace.name},
        "membership": {
            "id": membership.id,
            "organization_id": membership.org_id,
            "user_id": membership.user_id,
            "principal_id": membership.principal_id,
            "status": membership.status.value,
        },
    }


def _create(
    service: AccountOrganizationService,
    request: Request,
    response: Response,
    payload: AccountOrganizationRequest,
    idempotency_key: str,
) -> dict[str, Any]:
    """Run the use case for the account the middleware authenticated."""
    if not creation_enabled():
        raise HTTPException(
            status_code=404, detail="Account organization creation is disabled"
        )
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="Account session required")
    try:
        command = AccountOrganizationCommand.build(**payload.model_dump())
        result = service.create(
            account,
            command,
            idempotency_key,
            request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LoginThrottled as exc:
        raise throttled_response(exc) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="This Idempotency-Key was already used with a different request",
        ) from exc
    except OrganizationLimitReached as exc:
        raise HTTPException(
            status_code=403,
            detail="This account has reached its organization limit",
        ) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Account is not active") from exc
    response.status_code = 201 if result.created else 200
    response.headers["Cache-Control"] = "no-store"
    return _view(result)


def register_account_organization_routes(
    app: FastAPI, service: AccountOrganizationService
) -> None:
    """Register the bearer route and its cookie twin (CSRF is enforced upstream)."""

    @app.post("/api/v0/account/organizations", status_code=201)
    def create_account_organization(
        payload: AccountOrganizationRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _create(service, request, response, payload, idempotency_key)

    @app.post("/api/v0/account/browser/organizations", status_code=201)
    def create_account_browser_organization(
        payload: AccountOrganizationRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        return _create(service, request, response, payload, idempotency_key)
