"""Compose application ports with the selected infrastructure adapters."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import partial
from typing import Literal, cast

import httpx

from application.ports.limits import StaticLimitPolicy, limit_policy_from_env
from application.ports.product_repositories import ProductRepositorySet
from application.ports.secrets import SecretStore
from application.ports.throttling import LoginRateLimiter
from application.ports.usage import UsageLedger
from apps.limits_wiring import build_admission_service
from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_adapters import (
    build_login_rate_limiter,
    build_runtime_adapters,
    build_secret_store,
    build_slack_store,
    build_usage_ledger,
)
from apps.runtime_service_composition import configure_capability_services
from apps.slack_lifecycle_wiring import wire_slack_lifecycle
from contracts.v1.base import ProductContract
from infrastructure.config.settings import PoolSettings, load_pool_settings
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.queue_metrics import publish_queue_gauges
from infrastructure.database.store import ProductPostgresStore
from infrastructure.http.model_egress import open_model_http_client
from infrastructure.http.pool import open_http_client
from infrastructure.persistence.memory_repository import InMemoryRepository
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from infrastructure.persistence.unit_of_work import InMemoryUnitOfWork
from modules.platform_controls.audit import OperatorAudit
from modules.platform_controls.controls import PlatformControls
from modules.platform_controls.store import (
    ControlsStore,
    MemoryControlsStore,
    PostgresControlsStore,
)
from modules.recovery.restore_gate import assert_from_env
from modules.usage.service import UsageSettings
from shared.limits_config import LimitsConfig
from shared.metrics import register_collector
from shared.request_context import require_tenant_context


def open_process_runtime(
    *, process: Literal["api", "worker", "cli"], app_role: str | None = None
) -> LocalPlatformRuntime:
    """Open process-scoped pools and compose the application runtime.

    ``app_role`` overrides ``WEAVES_DB_APP_ROLE``; an empty string keeps the
    owner connection, which operator tooling uses to bypass tenant policies.
    """
    if process != "cli":
        # A restored database is never served before reconcile completes.
        assert_from_env()
    settings = load_pool_settings(process=process)
    local_seeds = os.environ.get("WEAVES_SEED_LOCAL_DEFAULTS", "true").strip().lower()
    if local_seeds not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("WEAVES_SEED_LOCAL_DEFAULTS must be true or false")
    http_client = open_http_client(settings)
    store: ProductPostgresStore | None = None
    try:
        store = _open_store(settings, app_role=app_role)
        if store is not None and process == "worker":
            register_collector(partial(publish_queue_gauges, store))
        seeds_local = local_seeds in {"true", "1", "yes"}
        runtime = compose_platform_runtime(
            http_client=http_client,
            store=store,
            seed_local_defaults=seeds_local,
            limit_policy=limit_policy_from_env(os.environ),
        )
        # Durable deployments without local demo data never fall back to
        # local-org/local-workspace; every tenant operation needs real context.
        require_tenant_context(store is not None and not seeds_local)
        return runtime
    except Exception:
        http_client.close()
        if store is not None:
            store.close()
        raise


def _open_store(
    settings: PoolSettings, app_role: str | None = None
) -> ProductPostgresStore | None:
    """Open a process-owned database pool when database settings are present."""
    if not settings.database_url:
        return None
    pool = open_postgres_pool(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        application_name=settings.db_application_name,
    )
    return ProductPostgresStore(pool=pool, app_role=app_role)


def compose_platform_runtime(
    *,
    http_client: httpx.Client,
    store: ProductPostgresStore | None,
    seed_local_defaults: bool = True,
    limit_policy: StaticLimitPolicy | None = None,
) -> LocalPlatformRuntime:
    """Build persistence, adapter, and lifecycle dependencies for one process."""
    policy = limit_policy or StaticLimitPolicy(LimitsConfig())
    repositories = create_product_repository_set(store)
    secret_store = build_secret_store(store)
    login_limiter = build_login_rate_limiter(store)
    provider_http_client = open_model_http_client()
    try:
        return _compose_with_model_client(
            http_client,
            provider_http_client,
            store,
            seed_local_defaults,
            repositories,
            secret_store,
            login_limiter,
            build_usage_ledger(store),
            policy,
        )
    except Exception:
        provider_http_client.close()
        raise


def _compose_with_model_client(
    http_client: httpx.Client,
    provider_http_client: httpx.Client,
    store: ProductPostgresStore | None,
    seed_local_defaults: bool,
    repositories: ProductRepositorySet,
    secret_store: SecretStore,
    login_limiter: LoginRateLimiter,
    usage_ledger: UsageLedger,
    policy: StaticLimitPolicy,
) -> LocalPlatformRuntime:
    """Bind both process-owned pools and transfer their lifetime to the runtime."""
    adapters = build_runtime_adapters(
        http_client=http_client,
        provider_http_client=provider_http_client,
        secret_store=secret_store,
        repositories=repositories,
    )
    unit_of_work = _build_unit_of_work(store, repositories)
    runtime = LocalPlatformRuntime(
        http_client=http_client,
        provider_http_client=provider_http_client,
        storage_mode="postgres" if store is not None else "memory",
        seed_local_defaults=seed_local_defaults,
        repository_set=repositories,
        secret_store=secret_store,
        login_rate_limiter=login_limiter,
        usage_ledger=usage_ledger,
        usage_settings=UsageSettings(
            chars_per_token=policy.config.reservation_input_chars_per_token,
            retention_days=policy.config.usage_retention_days,
        ),
        runtime_adapters=adapters,
        unit_of_work=unit_of_work,
        advisory_transaction_lock=(
            store.acquire_advisory_transaction_lock if store is not None else None
        ),
        close_storage=store.close if store is not None else _noop,
        check_storage=store.check if store is not None else _noop,
    )
    controls_store: ControlsStore = (
        PostgresControlsStore(store) if store is not None else MemoryControlsStore()
    )
    runtime.controls_store = controls_store
    runtime.slack_store = build_slack_store(store)
    runtime.operator_audit = OperatorAudit(controls_store)
    runtime.admission = build_admission_service(
        store=store,
        repositories=repositories,
        lock=store.acquire_advisory_transaction_lock if store is not None else None,
        controls_store=controls_store,
    )
    runtime.platform_controls = cast(PlatformControls, runtime.admission.controls)
    capability_services = configure_capability_services(runtime)
    runtime.attach_capability_services(capability_services)
    wire_slack_lifecycle(runtime)
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
