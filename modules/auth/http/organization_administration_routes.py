"""Organization administration uses validated middleware scope."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from modules.auth.http.account_organization_routes import slug_refusal
from modules.auth.organization_administration import OrganizationAdministrationService
from modules.auth.organization_guard import AdministrationConflict
from modules.auth.organization_slugs import SlugRejected, SlugTaken


class SettingsUpdate(BaseModel):
    """A complete preferences edit with optimistic concurrency control."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    timezone: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=2, max_length=35)
    slug: str | None = Field(default=None, max_length=64)
    expected_revision: int = Field(ge=1)


class OwnershipTransfer(BaseModel):
    """Explicitly confirmed ownership transfer against a known revision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    principal_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    confirmation: str = Field(min_length=1, max_length=160)


def _perform[T](operation: Callable[[], T]) -> T:
    try:
        return operation()
    except SlugRejected:
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="Organization member not found"
        ) from exc
    except AdministrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def register_organization_administration_routes(
    app: FastAPI,
    service: OrganizationAdministrationService,
) -> None:
    """Expose preferences, explicit ownership transfer and tenant membership removal."""

    @app.get("/api/v0/organization/settings")
    def get_settings(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return _perform(service.get)

    @app.patch("/api/v0/organization/settings")
    def update_settings(payload: SettingsUpdate) -> Any:
        try:
            return _perform(lambda: service.update(**payload.model_dump()))
        except (SlugRejected, SlugTaken) as exc:
            return slug_refusal(exc)

    @app.post("/api/v0/organization/transfer-ownership")
    def transfer_ownership(
        payload: OwnershipTransfer, request: Request
    ) -> dict[str, Any]:
        if getattr(request.state, "account", None) is None:
            raise HTTPException(
                status_code=403,
                detail="An account sign-in is required to transfer ownership",
            )
        return _perform(lambda: service.transfer(**payload.model_dump()))

    @app.delete("/api/v0/organization/members/{user_id}", status_code=204)
    def remove_member(user_id: str) -> Response:
        _perform(lambda: service.remove_member(user_id))
        return Response(status_code=204)
