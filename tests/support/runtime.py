"""Build fresh platform runtimes for tests through the app composition path."""

from __future__ import annotations

import httpx

from application.platform import LocalPlatformRuntime as PlatformRuntime
from apps.api.wiring import _open_store
from apps.runtime_wiring import compose_platform_runtime
from infrastructure.config.settings import load_pool_settings
from infrastructure.database.store import ProductPostgresStore
from infrastructure.http.pool import open_http_client

_UNSET = object()


def LocalPlatformRuntime(
    *,
    http_client: httpx.Client | None = None,
    postgres_store: ProductPostgresStore | object | None = _UNSET,
) -> PlatformRuntime:
    """Create a runtime with test overrides while preserving environment defaults."""
    settings = load_pool_settings(process="api")
    client = http_client or open_http_client(settings)
    store = _open_store(settings) if postgres_store is _UNSET else postgres_store
    if store is not None and not isinstance(store, ProductPostgresStore):
        client.close()
        raise TypeError("postgres_store must be a ProductPostgresStore or None")
    try:
        return compose_platform_runtime(http_client=client, store=store)
    except Exception:
        client.close()
        if store is not None:
            store.close()
        raise
