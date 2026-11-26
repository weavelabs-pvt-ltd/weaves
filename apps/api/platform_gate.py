"""Refuse writes while the platform is in operator-set read-only mode.

``read_only`` (see ``modules/platform_controls``) answers every state-changing
request under ``/api/v0/`` (POST, PUT, PATCH, DELETE, including webhooks) with
HTTP 503 and the stable code ``platform_read_only``, and a ``Retry-After``.
Reads, health checks and CORS preflights are served. The switch is read from a
cache of about five seconds, so it takes effect without a restart.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from modules.platform_controls.controls import ControlsSnapshot, PlatformControls
from modules.platform_controls.errors import READ_ONLY_CODE, PlatformUnavailable

logger = logging.getLogger("product.controls")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Slack events keep arriving during maintenance: Ask stays allowed under
# read-only (design 3.9) and a refusal would only make Slack retry and, after
# enough failures, disable the subscription.
EXEMPT_PATHS = frozenset({"/api/v0/slack/events"})
API_PREFIX = "/api/v0/"
DEFAULT_MESSAGE = (
    "The service is in read-only maintenance. Your data is safe; "
    "please try again shortly."
)


def unavailable_response(exc: PlatformUnavailable) -> JSONResponse:
    """The 503 body: the API's ``detail`` shape plus a stable ``code``."""
    return JSONResponse(
        status_code=503,
        content={"detail": exc.message, "code": exc.code},
        headers={
            "Cache-Control": "no-store",
            "Retry-After": str(max(1, exc.retry_after_seconds)),
        },
    )


async def _snapshot(controls: PlatformControls) -> ControlsSnapshot | None:
    """Fresh cache hit, else one threadpool read; never raises."""
    hit = controls.cached()
    if hit is not None:
        return hit
    try:
        return await run_in_threadpool(controls.snapshot)
    except Exception:
        logger.exception("platform controls lookup failed; serving request")
        return None


def install_platform_gate(app: FastAPI) -> None:
    """Install the read-only gate (inside authentication, so ids are bound)."""

    @app.middleware("http")
    async def platform_gate(request: Any, call_next: Any) -> Any:
        controls: PlatformControls | None = getattr(
            request.app.state, "platform_controls", None
        )
        if (
            controls is None
            or request.method in SAFE_METHODS
            or not request.url.path.startswith(API_PREFIX)
            or request.url.path in EXEMPT_PATHS
        ):
            return await call_next(request)
        snapshot = await _snapshot(controls)
        if snapshot is not None and snapshot.read_only:
            return unavailable_response(
                PlatformUnavailable(READ_ONLY_CODE, snapshot.message or DEFAULT_MESSAGE)
            )
        return await call_next(request)
