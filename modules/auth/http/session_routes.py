"""Identity probe and service-account API routes."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.http.responses import DumpModel, credential_view
from modules.auth.http.schemas import ServiceAccountCreateRequest
from shared.request_context import effective_principal_id, effective_workspace_id


def _register_get_authenticated_user(
    app: FastAPI,
    authentication_service: AuthenticationService,
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
        try:
            permissions = credential_service.effective_permissions(principal.id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {
            "principal": _dump(principal),
            "user": _dump(user) if user else None,
            "workspace_id": effective_workspace_id(),
            "effective_permissions": [permission.value for permission in permissions],
            "organization_permissions": [
                p.value
                for p in credential_service.organization_permissions(principal.id)
            ],
        }


def _register_create_service_account(
    app: FastAPI,
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
    credential_service: CredentialService,
    dump: DumpModel,
) -> None:
    """Register the identity probe and service-account routes."""
    _register_get_authenticated_user(
        app,
        authentication_service,
        credential_service,
        dump,
    )
    _register_create_service_account(
        app,
        credential_service,
        dump,
    )
