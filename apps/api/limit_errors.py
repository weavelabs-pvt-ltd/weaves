"""Map limit refusals onto HTTP 429 with the stable ``limit_exceeded`` code.

The body keeps the API's ``{"detail": ...}`` error shape and adds the code and
the caller's own ``resource``, ``limit`` and ``used`` (omitted for global
switches, which must not disclose other tenants). ``Retry-After`` appears only
when waiting can help.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from modules.limits.errors import CODE, LimitExceeded


def limit_response(exc: LimitExceeded) -> JSONResponse:
    """Build the 429 response for one refusal."""
    content: dict[str, Any] = {
        "detail": exc.message,
        "code": CODE,
        "resource": exc.resource,
    }
    if exc.limit is not None:
        content["limit"] = exc.limit
    if exc.used is not None:
        content["used"] = exc.used
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after_seconds)))
    return JSONResponse(status_code=429, content=content, headers=headers)


def install_limit_error_handlers(app: FastAPI) -> None:
    """Register the 429 mapping for ``LimitExceeded``."""

    @app.exception_handler(LimitExceeded)
    async def limit_exceeded_handler(_: Request, exc: LimitExceeded) -> JSONResponse:
        return limit_response(exc)
