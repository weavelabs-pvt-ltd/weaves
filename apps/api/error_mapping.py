"""Map application and domain errors onto HTTP responses."""

from typing import Any

from fastapi import FastAPI
from starlette.responses import JSONResponse


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers for errors raised by product use cases."""

    @app.exception_handler(PermissionError)
    async def permission_error_handler(_: Any, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})
