"""Process-scoped synchronous HTTP client."""

from __future__ import annotations

import httpx

from infrastructure.config.settings import PoolSettings, load_pool_settings


def open_http_client(settings: PoolSettings | None = None) -> httpx.Client:
    """Open one sync client with explicit limits. The caller closes it."""
    resolved = settings or load_pool_settings(process="api")
    return httpx.Client(
        timeout=httpx.Timeout(resolved.http_timeout_seconds),
        limits=httpx.Limits(
            max_connections=resolved.http_max_connections,
            max_keepalive_connections=resolved.http_max_keepalive_connections,
        ),
    )
