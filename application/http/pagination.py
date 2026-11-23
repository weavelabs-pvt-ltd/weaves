"""FastAPI dependencies that parse the shared paging query parameters."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query

from application.pagination import (
    PageRequest,
    RecentPageRequest,
    page_request,
    recent_page_request,
)

_LIMIT = Query(
    default=None,
    ge=1,
    le=1_000,
    description="Maximum rows to return (server maximum applies).",
)


def page_params(
    limit: int | None = _LIMIT,
    cursor: str | None = Query(default=None, description="Opaque keyset cursor."),
    paginate: bool = Query(
        default=False, description="Return {items, next_cursor} on the first page."
    ),
) -> PageRequest:
    """Validated ``limit``/``cursor``/``paginate`` of one catalog list request."""
    try:
        return page_request(limit, cursor, paginate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def recent_page_params(
    limit: int | None = _LIMIT,
    cursor: str | None = Query(default=None, description="Opaque keyset cursor."),
    paginate: bool = Query(
        default=False, description="Return {items, next_cursor} on the first page."
    ),
) -> RecentPageRequest:
    """Validated paging parameters of a newest-first collection."""
    try:
        return recent_page_request(limit, cursor, paginate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


PageParams = Annotated[PageRequest, Depends(page_params)]
RecentPageParams = Annotated[RecentPageRequest, Depends(recent_page_params)]
