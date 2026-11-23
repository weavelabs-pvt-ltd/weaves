"""Internal Prometheus-style metrics endpoint, never served to the public.

The route answers only direct peers on the Docker network: any request that
carries a proxy header (``Forwarded`` or ``X-Forwarded-*``) is refused with a
plain 404, the public Caddy configuration additionally blocks the path, and an
optional ``WEAVES_METRICS_TOKEN`` adds a bearer check. Output has no tenant,
principal or request identifiers.
"""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from shared.metrics import REGISTRY

METRICS_PATH = "/api/v0/internal/metrics"
PROXY_HEADER_PREFIXES = ("x-forwarded-", "x-real-ip", "forwarded", "via")
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _proxied(request: Request) -> bool:
    return any(
        name.lower().startswith(PROXY_HEADER_PREFIXES) for name in request.headers
    )


def _token_refused(request: Request) -> bool:
    expected = os.environ.get("WEAVES_METRICS_TOKEN", "").strip()
    if not expected:
        return False
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    return scheme.casefold() != "bearer" or not hmac.compare_digest(
        supplied.encode(), expected.encode()
    )


def install_metrics(app: FastAPI) -> None:
    """Register the internal metrics route (hidden from the OpenAPI schema)."""

    @app.get(METRICS_PATH, include_in_schema=False)
    def metrics(request: Request) -> Response:
        if _proxied(request) or _token_refused(request):
            return PlainTextResponse("Not Found", status_code=404)
        return PlainTextResponse(REGISTRY.render(), media_type=CONTENT_TYPE)
