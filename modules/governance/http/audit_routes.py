"""Audit event search and export endpoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel

from modules.governance.service import GovernanceService


def register_audit_routes(
    app: FastAPI,
    governance: GovernanceService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register bounded audit search and NDJSON export endpoints."""

    @app.get("/api/v0/audit-events")
    def list_audit_events(
        limit: int = Query(default=50, ge=1, le=100),
        action: str | None = Query(default=None, min_length=1, max_length=128),
        target_type: str | None = Query(default=None, min_length=1, max_length=128),
        target_id: str | None = Query(default=None, min_length=1, max_length=128),
        actor_principal_id: str | None = Query(
            default=None, min_length=1, max_length=128
        ),
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        try:
            events = governance.search_audit_events(
                action=action,
                target_type=target_type,
                target_id=target_id,
                actor_principal_id=actor_principal_id,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return [dump(event) for event in events]

    @app.get("/api/v0/audit-events/export")
    def export_audit_events(
        limit: int = Query(default=500, ge=1, le=1000),
        cursor: str | None = Query(default=None, max_length=1024),
        action: str | None = Query(default=None, min_length=1, max_length=128),
        target_type: str | None = Query(default=None, min_length=1, max_length=128),
        target_id: str | None = Query(default=None, min_length=1, max_length=128),
        actor_principal_id: str | None = Query(
            default=None, min_length=1, max_length=128
        ),
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> Response:
        """Export one bounded NDJSON page; repeat with its cursor to continue."""
        try:
            events = governance.search_audit_events(
                action=action,
                target_type=target_type,
                target_id=target_id,
                actor_principal_id=actor_principal_id,
                created_after=created_after,
                created_before=created_before,
                limit=limit + 1,
                cursor=cursor,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        page = events[:limit]
        headers = {"Cache-Control": "no-store"}
        if len(events) > limit and page:
            headers["X-Next-Cursor"] = governance.audit_event_cursor(page[-1])
        return Response(
            content="".join(
                json.dumps(dump(event), separators=(",", ":")) + "\n" for event in page
            ),
            media_type="application/x-ndjson",
            headers=headers,
        )
