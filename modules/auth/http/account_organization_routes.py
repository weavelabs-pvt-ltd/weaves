"""Create an organization from an authenticated account session."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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
from modules.auth.organization_email_domains import (
    DOMAIN_CODE,
    DomainBelongsToOrganization,
)
from modules.auth.organization_slugs import SlugRejected, SlugTaken
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
    slug: str | None = Field(default=None, max_length=64)


def slug_refusal(exc: SlugRejected | SlugTaken) -> JSONResponse:
    """422 for a malformed or reserved slug, 409 for a taken one, with a suggestion."""
    return JSONResponse(
        status_code=409 if isinstance(exc, SlugTaken) else 422,
        content={
            "detail": (
                "That URL is already taken" if isinstance(exc, SlugTaken) else str(exc)
            ),
            "code": f"slug_{exc.reason}",
            "reason": exc.reason,
            "slug": exc.slug,
            "suggestion": exc.suggestion,
        },
        headers={"Cache-Control": "no-store"},
    )


def _throttled(exc: LoginThrottled) -> HTTPException:
    """429 with the standard Retry-After header and the wait in the message."""
    seconds = exc.retry_after_seconds
    return HTTPException(
        status_code=429,
        detail=f"Too many requests. Try again in {seconds} seconds.",
        headers={"Retry-After": str(seconds)},
    )


def _account(request: Request) -> Account:
    account = getattr(request.state, "account", None)
    if not isinstance(account, Account):
        raise HTTPException(status_code=401, detail="Account session required")
    return account


def _address(request: Request) -> str | None:
    return request.client.host if request.client else None


def _view(result: AccountOrganizationResult) -> dict[str, Any]:
    """The tenant is returned without any bearer token or API credential."""
    organization, workspace, membership, settings = (
        result.organization,
        result.workspace,
        result.membership,
        result.settings,
    )
    return {
        "organization": {
            "id": organization.id,
            "name": organization.name,
            "status": organization.status.value,
            "slug": settings.slug,
            "timezone": settings.timezone,
            "locale": settings.locale,
            "email_domain": settings.email_domain,
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
) -> Any:
    """Run the use case for the account the middleware authenticated."""
    if not creation_enabled():
        raise HTTPException(
            status_code=404, detail="Account organization creation is disabled"
        )
    account = _account(request)
    try:
        command = AccountOrganizationCommand.build(**payload.model_dump())
        result = service.create(account, command, idempotency_key, _address(request))
    except SlugRejected as exc:
        return slug_refusal(service.explain_slug(exc))
    except SlugTaken as exc:
        return slug_refusal(exc)
    except DomainBelongsToOrganization as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"{exc.claim.organization_name} already uses Weaves with "
                    f"{exc.claim.domain}. Ask an admin to add you."
                ),
                "code": DOMAIN_CODE,
                "organization_name": exc.claim.organization_name,
            },
            headers={"Cache-Control": "no-store"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LoginThrottled as exc:
        raise _throttled(exc) from exc
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


def _check_slug(
    service: AccountOrganizationService, request: Request, response: Response, slug: str
) -> dict[str, Any]:
    """Answer availability for the signed-in account; names no organization."""
    try:
        answer = service.check_slug(_account(request), slug, _address(request))
    except LoginThrottled as exc:
        raise _throttled(exc) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Account is not active") from exc
    response.headers["Cache-Control"] = "no-store"
    return answer


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
    ) -> Any:
        return _create(service, request, response, payload, idempotency_key)

    @app.get("/api/v0/account/organization-slugs/{slug}")
    def check_organization_slug(
        slug: str, request: Request, response: Response
    ) -> dict[str, Any]:
        return _check_slug(service, request, response, slug)

    @app.get("/api/v0/account/browser/organization-slugs/{slug}")
    def check_browser_organization_slug(
        slug: str, request: Request, response: Response
    ) -> dict[str, Any]:
        return _check_slug(service, request, response, slug)

    @app.post("/api/v0/account/browser/organizations", status_code=201)
    def create_account_browser_organization(
        payload: AccountOrganizationRequest,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
    ) -> Any:
        return _create(service, request, response, payload, idempotency_key)
