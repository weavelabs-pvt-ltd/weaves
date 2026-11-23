"""One structured access line and one latency observation per API request."""

from __future__ import annotations

import logging

from starlette.requests import Request

from shared.metrics import HTTP_REQUEST_SECONDS, status_class

_log = logging.getLogger("product.api")
UNMATCHED_ROUTE = "unmatched"


def route_template(request: Request) -> str:
    """The matched route's template (``/tasks/{task_id}``), never the raw path.

    Raw paths carry identifiers; templates are a small fixed set, so they are safe
    as a metric label and as a log field.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


def record_request(request: Request, status: int, seconds: float) -> None:
    """Log the finished request and observe its duration."""
    template = route_template(request)
    HTTP_REQUEST_SECONDS.observe(
        seconds, route_template=template, status_class=status_class(status)
    )
    level = logging.ERROR if status >= 500 else logging.INFO
    _log.log(
        level,
        "request completed",
        extra={
            "event": "http_request",
            "method": request.method,
            "route": template,
            "status": status,
            "duration_ms": round(seconds * 1000, 1),
        },
    )
