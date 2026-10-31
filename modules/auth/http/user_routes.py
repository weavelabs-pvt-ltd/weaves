"""Identity API route registrations migrated from the v0 API."""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from modules.auth.http.responses import DumpModel, credential_view, invitation_view
from modules.auth.http.schemas import (
    OrganizationOnboardingRequest,
    UserInvitationCreateRequest,
    WorkspaceUserCreateRequest,
)
from modules.auth.invitation_service import InvitationService
from modules.auth.organization_service import OrganizationService
from modules.auth.user_administration_service import UserAdministrationService
from shared.errors import RateLimitExceeded


def _register_list_users(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the list users endpoint."""
    _dump = dump

    @app.get("/api/v0/users")
    def list_users() -> list[dict[str, Any]]:
        return [
            _dump(user) for user in user_administration_service.list_workspace_users()
        ]


def _register_create_workspace_user(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the create workspace user endpoint."""
    _dump = dump

    @app.post("/api/v0/users", status_code=201)
    def create_workspace_user(
        request: WorkspaceUserCreateRequest,
    ) -> dict[str, Any]:
        try:
            user, principal, role, credential, token = (
                user_administration_service.create_workspace_user(
                    email=request.email,
                    display_name=request.display_name,
                    permissions=tuple(request.permissions or ()),
                    role_id=request.role_id,
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "user": _dump(user),
            "principal": _dump(principal),
            "role": _dump(role),
            "credential": credential_view(credential),
            "access_token": token,
        }


def _register_list_user_invitations(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the list user invitations endpoint."""
    _dump = dump

    @app.get("/api/v0/user-invitations")
    def list_user_invitations() -> list[dict[str, Any]]:
        try:
            return [
                invitation_view(invitation, _dump)
                for invitation in invitation_service.list_user_invitations()
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def _register_create_user_invitation(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the create user invitation endpoint."""
    _dump = dump

    @app.post("/api/v0/user-invitations", status_code=201)
    def create_user_invitation(
        request: UserInvitationCreateRequest,
    ) -> dict[str, Any]:
        try:
            user, principal, role, invitation, token = (
                invitation_service.create_user_invitation(
                    email=request.email,
                    display_name=request.display_name,
                    permissions=tuple(request.permissions or ()),
                    role_id=request.role_id,
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Role not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "user": _dump(user),
            "principal": _dump(principal),
            "role": _dump(role),
            "invitation": invitation_view(invitation, _dump),
            "invitation_token": token,
        }


def _register_resend_user_invitation(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the resend user invitation endpoint."""
    _dump = dump

    @app.post("/api/v0/user-invitations/{invitation_id}/resend")
    def resend_user_invitation(invitation_id: str) -> dict[str, Any]:
        try:
            invitation, token = invitation_service.resend_user_invitation(invitation_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Invitation not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "invitation": invitation_view(invitation, _dump),
            "invitation_token": token,
        }


def _register_revoke_user_invitation(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the revoke user invitation endpoint."""
    _dump = dump

    @app.delete("/api/v0/user-invitations/{invitation_id}", status_code=204)
    def revoke_user_invitation(invitation_id: str) -> Response:
        try:
            invitation_service.revoke_user_invitation(invitation_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Invitation not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)


def _register_suspend_workspace_user(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the suspend workspace user endpoint."""
    _dump = dump

    @app.post("/api/v0/users/{user_id}/suspend")
    def suspend_workspace_user(user_id: str) -> dict[str, Any]:
        try:
            user = user_administration_service.suspend_workspace_user(user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        return _dump(user)


def _register_reactivate_workspace_user(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the reactivate workspace user endpoint."""
    _dump = dump

    @app.post("/api/v0/users/{user_id}/reactivate")
    def reactivate_workspace_user(user_id: str) -> dict[str, Any]:
        try:
            user, credential, token = (
                user_administration_service.reactivate_workspace_user(user_id)
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="User not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "user": _dump(user),
            "credential": credential_view(credential),
            "access_token": token,
        }


def _register_create_organization(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register the create organization endpoint."""
    _dump = dump

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
        ).lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise HTTPException(
                status_code=404,
                detail="Organization onboarding is disabled",
            )
        expected_onboarding_token = os.environ.get(
            "WEAVES_ORGANIZATION_ONBOARDING_TOKEN", ""
        ).strip()
        if expected_onboarding_token and not hmac.compare_digest(
            onboarding_token or "", expected_onboarding_token
        ):
            raise HTTPException(status_code=404, detail="Organization not found")
        try:
            organization_service.check_onboarding_rate_limit(
                request.client.host if request.client else None
            )
            organization, workspace, user, principal, token = (
                organization_service.create_organization(
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
            "organization": _dump(organization),
            "workspace": _dump(workspace),
            "owner": _dump(user),
            "principal": _dump(principal),
            "access_token": token,
        }


def register_user_routes(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    dump: DumpModel,
) -> None:
    """Register user routes."""
    _register_list_users(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_create_workspace_user(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_list_user_invitations(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_create_user_invitation(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_resend_user_invitation(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_revoke_user_invitation(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_suspend_workspace_user(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_reactivate_workspace_user(
        app, user_administration_service, invitation_service, organization_service, dump
    )
    _register_create_organization(
        app, user_administration_service, invitation_service, organization_service, dump
    )
