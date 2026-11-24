"""Build the product HTTP application and wire its dependencies."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.authentication import install_authentication
from apps.api.error_mapping import REQUEST_ID_HEADER, install_error_handlers
from apps.api.limit_errors import install_limit_error_handlers
from apps.api.metrics import install_metrics
from apps.api.request_limits import install_request_limits
from apps.api.router import register_v0_routes
from apps.api.wiring import build_api_process_services
from apps.platform_runtime import LocalPlatformRuntime
from modules.auth.http.browser_sessions import CSRF_HEADER
from shared.logging_setup import configure_logging


def _configure_logging() -> None:
    """Install JSON logs unless ``WEAVES_LOG_FORMAT=text`` (idempotent)."""
    if os.environ.get("WEAVES_LOG_FORMAT", "json").strip().lower() != "text":
        configure_logging("api", level=os.environ.get("LOG_LEVEL", "INFO"))


def create_app(runtime: LocalPlatformRuntime | None = None) -> FastAPI:
    """Create the API without opening process resources before startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        process_services = build_api_process_services(runtime)
        try:
            if not getattr(app.state, "product_routes_registered", False):
                register_v0_routes(app, process_services.routes)
                app.state.product_routes_registered = True
            app.state.authentication_services = process_services.authentication
            app.state.limit_policy = process_services.routes.limit_policy
            process_services.bootstrap()
            yield
        finally:
            process_services.close()

    app = FastAPI(
        title="Weaves Platform API",
        version="0.1.0",
        description=(
            "Local v0 API for configuring and testing the Weaves platform path."
        ),
        lifespan=lifespan,
    )
    install_error_handlers(app)
    install_limit_error_handlers(app)
    # Inside authentication: the tenant identity is bound when it runs.
    install_request_limits(app)
    install_metrics(app)
    install_authentication(app)
    allowed_origins = tuple(
        origin.strip().rstrip("/")
        for origin in os.environ.get(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080",
        ).split(",")
        if origin.strip()
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "Idempotency-Key",
            "If-Match",
            "X-Workspace-ID",
            "X-Organization-Id",
            CSRF_HEADER,
        ],
        expose_headers=["Retry-After", REQUEST_ID_HEADER],
    )
    return app


_configure_logging()  # before uvicorn announces startup
app = create_app()
