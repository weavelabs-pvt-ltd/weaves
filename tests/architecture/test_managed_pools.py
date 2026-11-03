"""Process wiring owns the HTTP client and database pool."""

import httpx
from fastapi import FastAPI

from apps.api.wiring import build_platform_runtime as build_api_runtime
from apps.worker.wiring import build_platform_runtime as build_worker_runtime
from infrastructure.config.settings import load_pool_settings
from infrastructure.http.pool import open_http_client
from modules.integrations.http.oauth import register_oauth_routes
from modules.integrations.http.oauth_context import OAuthRouteDependencies
from modules.integrations.oauth_state_service import OAuthStateService
from tests.support.runtime import LocalPlatformRuntime


def test_pool_settings_keep_the_historical_defaults(monkeypatch) -> None:
    for name in (
        "DATABASE_URL",
        "WEAVES_DB_POOL_MIN_SIZE",
        "WEAVES_DB_POOL_MAX_SIZE",
        "WEAVES_DB_APPLICATION_NAME",
        "WEAVES_HTTP_TIMEOUT_SECONDS",
        "WEAVES_HTTP_MAX_CONNECTIONS",
        "WEAVES_HTTP_MAX_KEEPALIVE_CONNECTIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    api = load_pool_settings(process="api")
    worker = load_pool_settings(process="worker")
    assert api.db_pool_min_size == 1
    assert api.db_pool_max_size == 10
    assert api.http_timeout_seconds == 5.0
    assert api.http_max_connections == 100
    assert api.http_max_keepalive_connections == 20
    assert api.db_application_name == "weaves-product-api"
    assert worker.db_application_name == "weaves-product-worker"


def test_pool_settings_reject_inverted_limits(monkeypatch) -> None:
    monkeypatch.setenv("WEAVES_DB_POOL_MIN_SIZE", "8")
    monkeypatch.setenv("WEAVES_DB_POOL_MAX_SIZE", "2")
    try:
        load_pool_settings(process="api")
    except ValueError as exc:
        assert "WEAVES_DB_POOL_MIN_SIZE" in str(exc)
    else:
        raise AssertionError("inverted database pool limits were accepted")


def test_http_client_sets_explicit_timeout_and_pool_limits() -> None:
    client = open_http_client()
    try:
        assert client.timeout == httpx.Timeout(5.0)
        pool = client._transport._pool
        assert pool._max_connections == 100
        assert pool._max_keepalive_connections == 20
    finally:
        client.close()


def test_runtime_closes_the_injected_http_client() -> None:
    client = httpx.Client()
    runtime = LocalPlatformRuntime(http_client=client, postgres_store=None)
    assert runtime.http_client is client
    assert runtime.storage_mode == "memory"
    runtime.close()
    assert client.is_closed


def test_api_and_worker_wiring_inject_one_http_client(monkeypatch) -> None:
    api_client = httpx.Client()
    worker_client = httpx.Client()
    clients = {
        "weaves-product-api": api_client,
        "weaves-product-worker": worker_client,
    }
    monkeypatch.setattr(
        "apps.runtime_wiring.open_http_client",
        lambda settings: clients[settings.db_application_name],
    )
    monkeypatch.setattr("apps.runtime_wiring._open_store", lambda settings: None)
    api_runtime = build_api_runtime()
    worker_runtime = build_worker_runtime()
    try:
        assert api_runtime.http_client is api_client
        assert worker_runtime.http_client is worker_client
    finally:
        api_runtime.close()
        worker_runtime.close()


def test_oauth_routes_reuse_the_runtime_http_client(monkeypatch) -> None:
    client = httpx.Client()
    runtime = LocalPlatformRuntime(http_client=client, postgres_store=None)

    def reject_new_client(*_args: object, **_kwargs: object) -> httpx.Client:
        raise AssertionError("OAuth opened its own HTTP client")

    monkeypatch.setattr(httpx, "Client", reject_new_client)
    dependencies = OAuthRouteDependencies(
        http_client=runtime.http_client,
        state_service=OAuthStateService(
            states=runtime.oauth_authorization_states,
            credential_service=runtime.credential_service,
        ),
        secret_store=runtime.secret_store,
        github_app_token_provider=runtime.github_app_token_provider,
        connection_service=runtime.integration_connection_service,
    )
    register_oauth_routes(FastAPI(), dependencies)
    runtime.close()
    assert client.is_closed
