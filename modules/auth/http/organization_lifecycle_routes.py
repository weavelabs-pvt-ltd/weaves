"""Owner lifecycle routes: state, deletion request/cancel and export.

Operator actions (suspend, resume, purge) are deliberately absent: they exist
only as scripts run on the VM and are never reachable over HTTP. The same routes
serve account bearer sessions and account cookies (CSRF and Origin are enforced
by the middleware); the owner, not a Customer Admin, may change anything.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from modules.auth.organization_export import ExportTooLarge, OrganizationExportService
from modules.auth.organization_guard import AdministrationConflict
from modules.auth.organization_lifecycle import OrganizationLifecycleService
from shared.request_context import effective_org_id


class DeletionRequest(BaseModel):
    """Owner confirmation naming the organization, with an optional reason."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    confirm_name: str = Field(min_length=1, max_length=160)
    reason: str | None = Field(default=None, max_length=500)


def _perform[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Organization not found") from exc
    except ExportTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except AdministrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_account(request: Request) -> None:
    if getattr(request.state, "account", None) is None:
        raise HTTPException(
            status_code=403, detail="An account sign-in is required for this action"
        )


def register_organization_lifecycle_routes(
    app: FastAPI,
    lifecycle: OrganizationLifecycleService,
    exporter: OrganizationExportService,
) -> None:
    """Expose lifecycle state, owner deletion request/cancel and export."""

    @app.get("/api/v0/organization/lifecycle")
    def get_lifecycle(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _perform(lifecycle.view)

    @app.post("/api/v0/organization/deletion-request")
    def request_deletion(
        payload: DeletionRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        _require_account(request)
        response.headers["Cache-Control"] = "no-store"
        return _perform(lambda: lifecycle.request_deletion(**payload.model_dump()))

    @app.post("/api/v0/organization/deletion-cancel")
    def cancel_deletion(request: Request, response: Response) -> dict[str, Any]:
        _require_account(request)
        response.headers["Cache-Control"] = "no-store"
        return _perform(lifecycle.cancel_deletion)

    @app.post("/api/v0/organization/export")
    def export_organization(request: Request) -> Response:
        _require_account(request)
        body = _perform(exporter.export)
        name = f"organization-export-{effective_org_id()}.json"
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
            },
        )
