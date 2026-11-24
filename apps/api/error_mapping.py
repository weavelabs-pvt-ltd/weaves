"""Map application and domain errors onto HTTP responses."""

import logging
from typing import Any

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from shared.clock import new_id
from shared.correlation import resolve_request_id

REQUEST_ID_HEADER = "X-Request-Id"
_log = logging.getLogger(__name__)


def assign_request_id(request: Request) -> str:
    """Give the request its reference and return it.

    A well-formed inbound ``X-Request-Id`` is kept so a caller can follow its own
    id through the logs; anything else is replaced with a generated opaque id.
    """
    request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
    request.state.request_id = request_id
    return request_id


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers for errors raised by product use cases."""

    @app.exception_handler(PermissionError)
    async def permission_error_handler(_: Any, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, _: Exception) -> JSONResponse:
        """Answer an unhandled failure with the reference a person can quote.

        The response leaves outside the request middleware, so the header is
        set here; the server still logs the traceback after this returns.
        """
        request_id = getattr(request.state, "request_id", None) or new_id()
        _log.error(
            "unhandled error method=%s path=%s",
            request.method,
            request.url.path,
            extra={"request_id": request_id, "event": "unhandled_error"},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={REQUEST_ID_HEADER: request_id},
        )
