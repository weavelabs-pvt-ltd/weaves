"""Build the product HTTP application and wire its dependencies."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.authentication import install_authentication
from apps.api.error_mapping import install_error_handlers
from apps.api.router import register_v0_routes
from apps.api.wiring import build_api_process_services
from apps.platform_runtime import LocalPlatformRuntime


def create_app(runtime: LocalPlatformRuntime | None = None) -> FastAPI:
    """Create the API without opening process resources before startup."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        process_services = build_api_process_services(runtime)
        try:
            if not getattr(app.state, "product_routes_registered", False):
                register_v0_routes(app, process_services.routes)
                app.state.product_routes_registered = True
            app.state.authentication_services = process_services.authentication
            process_services.bootstrap()
            process_services.purge_expired_memory()
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
            "X-Workspace-ID",
            "X-Weaves-Onboarding-Token",
        ],
    )
    return app


app = create_app()
