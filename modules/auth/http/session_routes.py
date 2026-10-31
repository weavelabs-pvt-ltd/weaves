"""Identity API route registrations migrated from the v0 API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.http.responses import DumpModel, credential_view
from modules.auth.http.schemas import (
    ServiceAccountCreateRequest,
    UserInvitationAcceptanceRequest,
    UserLoginRequest,
    UserPasswordRequest,
)
from modules.auth.invitation_service import InvitationService
from modules.auth.organization_service import OrganizationService
from shared.errors import LoginThrottled
from shared.request_context import effective_principal_id


def _register_accept_user_invitation(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the accept user invitation endpoint."""
    _dump = dump

    @app.post("/api/v0/auth/accept-invitation")
    def accept_user_invitation(
        request: UserInvitationAcceptanceRequest,
    ) -> dict[str, Any]:
        try:
            user, principal, session, token = invitation_service.accept_user_invitation(
                request.invitation_token.get_secret_value(),
                request.password.get_secret_value(),
            )
            organization_service.resolve_principal_scope(principal.id)
        except PermissionError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invitation is invalid, expired, or no longer has workspace access"
                ),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "user": _dump(user),
            "principal": _dump(principal),
            "session_id": session.id,
            "access_token": token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
        }


def _register_login_user(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the login user endpoint."""
    _dump = dump

    @app.post("/api/v0/auth/login")
    def login_user(payload: UserLoginRequest, request: Request) -> dict[str, Any]:
        try:
            user, principal = authentication_service.authenticate_user_password(
                payload.email,
                payload.password.get_secret_value(),
                org_id=payload.organization_id,
                client_address=(request.client.host if request.client else None),
            )
            organization_service.resolve_principal_scope(principal.id)
            session, token = authentication_service.create_user_session(principal)
        except LoginThrottled as exc:
            raise HTTPException(
                status_code=429,
                detail="Too many sign-in attempts. Try again later.",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except PermissionError as exc:
            raise HTTPException(
                status_code=401,
                detail="Invalid email, password, or account access",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        return {
            "session_id": session.id,
            "access_token": token,
            "token_type": "bearer",
            "expires_at": session.expires_at,
            "user": _dump(user),
            "principal": _dump(principal),
        }


def _register_set_authenticated_user_password(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the set authenticated user password endpoint."""
    _dump = dump

    @app.put("/api/v0/auth/password", status_code=204)
    def set_authenticated_user_password(request: UserPasswordRequest) -> Response:
        try:
            authentication_service.set_user_password(
                request.password.get_secret_value(),
                current_password=(
                    request.current_password.get_secret_value()
                    if request.current_password is not None
                    else None
                ),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(status_code=204)


def _register_get_authenticated_user(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the get authenticated user endpoint."""
    _dump = dump

    @app.get("/api/v0/auth/me")
    def get_authenticated_user() -> dict[str, Any]:
        principal, user = authentication_service.get_authenticated_identity(
            effective_principal_id("local-developer")
        )
        return {"principal": _dump(principal), "user": _dump(user) if user else None}


def _register_logout_user(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the logout user endpoint."""
    _dump = dump

    @app.post("/api/v0/auth/logout", status_code=204)
    def logout_user(authorization: str | None = Header(default=None)) -> Response:
        scheme, separator, supplied_token = (authorization or "").partition(" ")
        if not separator or scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="A bearer session is required")
        try:
            authentication_service.revoke_user_session(
                supplied_token,
                requested_by_principal_id=effective_principal_id("local-developer"),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=400,
                detail="The supplied bearer credential is not an active user session",
            ) from exc
        return Response(status_code=204)


def _register_create_service_account(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the create service account endpoint."""
    _dump = dump

    @app.post("/api/v0/service-accounts", status_code=201)
    def create_service_account(request: ServiceAccountCreateRequest) -> dict[str, Any]:
        try:
            principal, role, credential, token = (
                credential_service.create_service_account(
                    name=request.name,
                    permissions=tuple(request.permissions),
                    expires_in_days=request.expires_in_days,
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "principal": _dump(principal),
            "role": _dump(role),
            "credential": credential_view(credential),
            "token": token,
        }


def register_session_routes(
    app: FastAPI,
    authentication_service: AuthenticationService,
    invitation_service: InvitationService,
    organization_service: OrganizationService,
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register session routes."""
    _register_accept_user_invitation(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
    _register_login_user(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
    _register_set_authenticated_user_password(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
    _register_get_authenticated_user(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
    _register_logout_user(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
    _register_create_service_account(
        app,
        authentication_service,
        invitation_service,
        organization_service,
        credential_service,
        dump,
    )
