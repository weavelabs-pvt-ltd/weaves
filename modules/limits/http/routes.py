"""Read-only organization limits route."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response

from modules.limits.view import OrganizationLimitsView


def register_limits_routes(app: FastAPI, view: OrganizationLimitsView) -> None:
    """Expose ``GET /api/v0/organization/limits`` (usage against limits)."""

    @app.get("/api/v0/organization/limits")
    def get_limits(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return view.get().model_dump(mode="json")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
