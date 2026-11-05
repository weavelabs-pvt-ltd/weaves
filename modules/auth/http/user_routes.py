"""Workspace member provisioning and lifecycle API routes."""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from modules.auth.http.responses import DumpModel
from modules.auth.http.schemas import (
    OrganizationOnboardingRequest,
    WorkspaceUserCreateRequest,
)
from modules.auth.organization_service import OrganizationService
from modules.auth.user_administration_service import UserAdministrationService
from shared.clock import utc_now
from shared.errors import RateLimitExceeded


def _register_list_users(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Expose active and suspended members visible to the caller."""

    @app.get("/api/v0/users")
    def list_users() -> list[dict[str, Any]]:
        members = service.list_workspace_members()
        sessions = (
            service.user_sessions.list_scoped(members[0][0].org_id) if members else ()
        )
        result = []
        for user, principal_id in members:
            history = [
                session for session in sessions if session.principal_id == principal_id
            ]
            active = any(
                str(session.status) == "active" and session.expires_at > utc_now()
                for session in history
            )
            result.append(
                {
                    **dump(user),
                    "principal_id": principal_id,
                    "account_status": "suspended"
                    if str(user.status) == "suspended"
                    else "signed_in"
                    if active
                    else "signed_out"
                    if history
                    else "never",
                    "last_signed_in_at": max(
                        (session.created_at for session in history), default=None
                    ),
                }
            )
        return result


def _register_create_user(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Provision member access immediately without an invitation lifecycle."""

    @app.post("/api/v0/users", status_code=201)
    def create_user(request: WorkspaceUserCreateRequest) -> dict[str, Any]:
        try:
            user, principal, role = service.create_workspace_user(
                email=request.email,
                display_name=request.display_name,
                permissions=tuple(request.permissions or ()),
                role_id=request.role_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Role not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "user": dump(user),
            "principal": dump(principal),
            "role": dump(role),
            "sign_in_method": "organization_email_code",
            "access_ready": True,
        }


def _register_suspend_user(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Disable member access and revoke active sessions and credentials."""

    @app.post("/api/v0/users/{user_id}/suspend")
    def suspend_user(user_id: str) -> dict[str, Any]:
        try:
            return dump(service.suspend_workspace_user(user_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc


def _register_reactivate_user(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Restore a suspended member's access; sign-in still requires email proof."""

    @app.post("/api/v0/users/{user_id}/reactivate")
    def reactivate_user(user_id: str) -> dict[str, Any]:
        try:
            user = service.reactivate_workspace_user(user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"user": dump(user), "sign_in_method": "organization_email_code"}


def _register_create_organization(
    app: FastAPI,
    service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register organization bootstrap with its initial administrator."""

    @app.post("/api/v0/onboarding/organizations", status_code=201)
    def create_organization(
        request: Request,
        onboarding_request: OrganizationOnboardingRequest,
        onboarding_token: str | None = Header(
            default=None, alias="X-Weaves-Onboarding-Token"
        ),
    ) -> dict[str, Any]:
        if os.environ.get(
            "WEAVES_ALLOW_ORGANIZATION_ONBOARDING", "true"
        ).lower() not in {"1", "true", "yes", "on"}:
            raise HTTPException(
                status_code=404, detail="Organization onboarding is disabled"
            )
        expected = os.environ.get("WEAVES_ORGANIZATION_ONBOARDING_TOKEN", "").strip()
        if expected and not hmac.compare_digest(onboarding_token or "", expected):
            raise HTTPException(status_code=404, detail="Organization not found")
        try:
            service.check_onboarding_rate_limit(
                request.client.host if request.client else None
            )
            organization, workspace, user, principal, token = (
                service.create_organization(
                    name=onboarding_request.name,
                    email=onboarding_request.owner_email,
                    display_name=onboarding_request.owner_display_name,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail="organization onboarding is temporarily limited",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        return {
            "organization": dump(organization),
            "workspace": dump(workspace),
            "owner": dump(user),
            "principal": dump(principal),
            "access_token": token,
        }


def register_user_routes(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register member administration and organization bootstrap routes."""
    _register_list_users(app, user_administration_service, dump)
    _register_create_user(app, user_administration_service, dump)
    _register_suspend_user(app, user_administration_service, dump)
    _register_reactivate_user(app, user_administration_service, dump)
    _register_create_organization(app, organization_service, dump)
