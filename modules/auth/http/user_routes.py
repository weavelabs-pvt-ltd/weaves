"""Workspace member provisioning and lifecycle API routes."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from modules.auth.http.responses import DumpModel
from modules.auth.http.schemas import WorkspaceUserCreateRequest
from modules.auth.user_administration_service import UserAdministrationService


def _register_list_users(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Expose members visible to the caller and whether each has linked an account."""

    @app.get("/api/v0/users")
    def list_users() -> list[dict[str, Any]]:
        return [
            {
                **dump(user),
                "principal_id": principal_id,
                "account_status": service.account_status(user),
            }
            for user, principal_id in service.list_workspace_members()
        ]


def _register_create_user(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Provision member access; the person links it by signing in with this email."""

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
            "sign_in_method": "google",
            "access_ready": True,
        }


def _register_suspend_user(
    app: FastAPI, service: UserAdministrationService, dump: DumpModel
) -> None:
    """Disable member access and revoke service credentials."""

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
        return {"user": dump(user), "sign_in_method": "google"}


def register_user_routes(
    app: FastAPI,
    user_administration_service: UserAdministrationService,
    dump: DumpModel,
) -> None:
    """Register member administration routes."""
    _register_list_users(app, user_administration_service, dump)
    _register_create_user(app, user_administration_service, dump)
    _register_suspend_user(app, user_administration_service, dump)
    _register_reactivate_user(app, user_administration_service, dump)
