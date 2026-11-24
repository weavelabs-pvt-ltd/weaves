"""``GET /api/v0/overview``: aggregate Home metrics."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query

from modules.overview.service import OverviewService


def register_overview_routes(app: FastAPI, service: OverviewService) -> None:
    """Register the Home metrics endpoint."""

    @app.get("/api/v0/overview")
    def overview(
        tz_offset_minutes: int = Query(
            default=0,
            ge=-840,
            le=840,
            description="Minutes east of UTC; shifts the per-day buckets.",
        ),
    ) -> dict[str, Any]:
        try:
            return service.overview(tz_offset_minutes=tz_offset_minutes)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
