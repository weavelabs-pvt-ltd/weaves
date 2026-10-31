"""Compose application ports with the selected infrastructure adapters."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Literal, cast

import httpx

from application.ports.product_repositories import ProductRepositorySet
from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_adapters import (
    build_login_rate_limiters,
    build_runtime_adapters,
    build_secret_store,
)
from apps.runtime_service_composition import configure_capability_services
from contracts.v1.base import ProductContract
from infrastructure.config.settings import PoolSettings, load_pool_settings
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.http.pool import open_http_client
from infrastructure.persistence.memory_repository import InMemoryRepository
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from infrastructure.persistence.unit_of_work import InMemoryUnitOfWork


def open_process_runtime(
    *, process: Literal["api", "worker", "cli"]
) -> LocalPlatformRuntime:
    """Open process-scoped pools and compose the application runtime."""
    settings = load_pool_settings(process=process)
    http_client = open_http_client(settings)
    store: ProductPostgresStore | None = None
    try:
        store = _open_store(settings)
        return compose_platform_runtime(http_client=http_client, store=store)
    except Exception:
        http_client.close()
        if store is not None:
            store.close()
        raise


def _open_store(settings: PoolSettings) -> ProductPostgresStore | None:
    """Open a process-owned database pool when database settings are present."""
    if not settings.database_url:
        return None
    pool = open_postgres_pool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        application_name=settings.db_application_name,
    )
    return ProductPostgresStore(pool=pool)


def compose_platform_runtime(
    *, http_client: httpx.Client, store: ProductPostgresStore | None
) -> LocalPlatformRuntime:
    """Build persistence, adapter, and lifecycle dependencies for one process."""
    repositories = create_product_repository_set(store)
    secret_store = build_secret_store(store)
    login_limiter, onboarding_limiter = build_login_rate_limiters(store)
    adapters = build_runtime_adapters(
        http_client=http_client,
        secret_store=secret_store,
        repositories=repositories,
    )
    unit_of_work = _build_unit_of_work(store, repositories)
    runtime = LocalPlatformRuntime(
        http_client=http_client,
        storage_mode="postgres" if store is not None else "memory",
        repository_set=repositories,
        secret_store=secret_store,
        login_rate_limiter=login_limiter,
        onboarding_rate_limiter=onboarding_limiter,
        runtime_adapters=adapters,
        unit_of_work=unit_of_work,
        advisory_transaction_lock=(
            store.acquire_advisory_transaction_lock if store is not None else None
        ),
        close_storage=store.close if store is not None else _noop,
        check_storage=store.check if store is not None else _noop,
    )
    capability_services = configure_capability_services(runtime)
    runtime.attach_capability_services(capability_services)
    return runtime


def _build_unit_of_work(
    store: ProductPostgresStore | None, repositories: ProductRepositorySet
) -> Callable[[], AbstractContextManager[None]]:
    """Select a database transaction or a snapshot transaction over memory repos."""
    if store is not None:
        return cast(Callable[[], AbstractContextManager[None]], store.transaction)
    memory_repositories = tuple(
        cast(InMemoryRepository[ProductContract], repository)
        for repository in repositories.all()
    )
    return cast(
        Callable[[], AbstractContextManager[None]],
        lambda: InMemoryUnitOfWork(memory_repositories),
    )


def _noop() -> None:
    """Represent a lifecycle action when the process uses in-memory storage."""
