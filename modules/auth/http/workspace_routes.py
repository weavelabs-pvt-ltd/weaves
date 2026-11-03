"""Identity API route registrations migrated from the v0 API."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response

from modules.auth.http.responses import DumpModel
from modules.auth.http.schemas import (
    RoleBindingCreateRequest,
    RoleCreateRequest,
    WorkspaceCreateRequest,
    WorkspacePatchRequest,
)
from modules.auth.organization_service import OrganizationService
from modules.auth.workspace_access_service import WorkspaceAccessService
from shared.request_context import effective_principal_id


def _register_list_organizations(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the list organizations endpoint."""
    _dump = dump

    @app.get("/api/v0/organizations")
    def list_organizations() -> list[dict[str, Any]]:
        return [_dump(organization_service.get_current_organization())]


def _register_list_workspaces(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the list workspaces endpoint."""
    _dump = dump

    @app.get("/api/v0/workspaces")
    def list_workspaces(
        include_archived: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        principal_id = effective_principal_id("local-developer")
        if include_archived:
            try:
                return [
                    _dump(item)
                    for item in workspace_access_service.list_managed_workspaces(
                        principal_id
                    )
                ]
            except PermissionError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        return [
            _dump(item)
            for item in workspace_access_service.list_principal_workspaces(principal_id)
        ]


def _register_create_workspace(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the create workspace endpoint."""
    _dump = dump

    @app.post("/api/v0/workspaces", status_code=201)
    def create_workspace(request: WorkspaceCreateRequest) -> dict[str, Any]:
        try:
            workspace = workspace_access_service.create_workspace(
                name=request.name,
                environment=request.environment,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(workspace)


def _register_update_workspace(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the update workspace endpoint."""
    _dump = dump

    @app.patch("/api/v0/workspaces/{workspace_id}")
    def update_workspace(
        workspace_id: str, patch: WorkspacePatchRequest
    ) -> dict[str, Any]:
        changes = patch.model_dump(exclude_unset=True)
        try:
            workspace = workspace_access_service.update_workspace(
                workspace_id, **changes
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workspace not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(workspace)


def _register_list_roles(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the list roles endpoint."""
    _dump = dump

    @app.get("/api/v0/roles")
    def list_roles() -> list[dict[str, Any]]:
        try:
            return [_dump(role) for role in workspace_access_service.list_roles()]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def _register_create_role(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the create role endpoint."""
    _dump = dump

    @app.post("/api/v0/roles", status_code=201)
    def create_role(request: RoleCreateRequest) -> dict[str, Any]:
        try:
            role = workspace_access_service.create_role(
                name=request.name,
                permissions=tuple(request.permissions),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(role)


def _register_list_role_bindings(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the list role bindings endpoint."""
    _dump = dump

    @app.get("/api/v0/role-bindings")
    def list_role_bindings() -> list[dict[str, Any]]:
        try:
            return [
                _dump(item) for item in workspace_access_service.list_role_bindings()
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def _register_create_role_binding(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the create role binding endpoint."""
    _dump = dump

    @app.post("/api/v0/role-bindings", status_code=201)
    def create_role_binding(
        request: RoleBindingCreateRequest,
    ) -> dict[str, Any]:
        try:
            binding = workspace_access_service.create_role_binding(
                principal_id=request.principal_id,
                role_id=request.role_id,
                workspace_id=request.workspace_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Principal or role not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(binding)


def _register_replace_role_binding(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register atomic role replacement at the requested assignment scope."""
    _dump = dump

    @app.put("/api/v0/role-bindings")
    def replace_role_binding(request: RoleBindingCreateRequest) -> dict[str, Any]:
        try:
            binding = workspace_access_service.replace_role_binding(
                principal_id=request.principal_id,
                role_id=request.role_id,
                workspace_id=request.workspace_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Principal or role not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(binding)


def _register_delete_role_binding(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register the delete role binding endpoint."""
    _dump = dump

    @app.delete("/api/v0/role-bindings/{binding_id}", status_code=204)
    def delete_role_binding(binding_id: str) -> Response:
        try:
            workspace_access_service.delete_role_binding(binding_id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Role binding not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=204)


def register_workspace_routes(
    app: FastAPI,
    organization_service: OrganizationService,
    workspace_access_service: WorkspaceAccessService,
    dump: DumpModel,
) -> None:
    """Register workspace routes."""
    _register_list_organizations(
        app, organization_service, workspace_access_service, dump
    )
    _register_list_workspaces(app, organization_service, workspace_access_service, dump)
    _register_create_workspace(
        app, organization_service, workspace_access_service, dump
    )
    _register_update_workspace(
        app, organization_service, workspace_access_service, dump
    )
    _register_list_roles(app, organization_service, workspace_access_service, dump)
    _register_create_role(app, organization_service, workspace_access_service, dump)
    _register_list_role_bindings(
        app, organization_service, workspace_access_service, dump
    )
    _register_create_role_binding(
        app, organization_service, workspace_access_service, dump
    )
    _register_replace_role_binding(
        app, organization_service, workspace_access_service, dump
    )
    _register_delete_role_binding(
        app, organization_service, workspace_access_service, dump
    )
