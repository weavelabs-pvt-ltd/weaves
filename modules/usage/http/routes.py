"""``GET /api/v0/usage``: the organization's own usage, bounded and read-only."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from contracts.v1 import UsageGroupBy
from modules.usage.queries import UsageQueries


class UsageQuery(BaseModel):
    """Query parameters; anything undeclared is rejected."""

    model_config = ConfigDict(extra="forbid")
    from_date: date | None = Field(default=None, alias="from")
    to_date: date | None = Field(default=None, alias="to")
    group_by: UsageGroupBy = UsageGroupBy.DAY


def register_usage_routes(app: FastAPI, queries: UsageQueries) -> None:
    """Expose the usage report to Owner and Admin principals."""

    @app.get("/api/v0/usage")
    def get_usage(
        params: Annotated[UsageQuery, Query()], response: Response
    ) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            summary = queries.summary(params.from_date, params.to_date, params.group_by)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return summary.model_dump(mode="json")
