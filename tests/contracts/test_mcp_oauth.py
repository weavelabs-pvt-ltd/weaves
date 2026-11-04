"""End-to-end OAuth setup for remote MCP connector installations."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from clients.integrations.oauth import OAuthTokenResolver
from modules.integrations.catalog import connector_catalog
from tests.support.runtime import LocalPlatformRuntime

_TOOL = {
    "name": "search_docs",
    "title": "Search docs",
    "description": "Search documents by query.",
    "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}


def test_mcp_oauth_authorize_select_and_install(monkeypatch) -> None:
    """Authorize an MCP server, select its read tool, and persist its token safely."""
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "tools.example.com,auth.example.com")
    monkeypatch.setenv("WEAVES_PUBLIC_API_BASE_URL", "https://api.weaves.test")
    seen: list[dict[str, object]] = []
    http_client = httpx.Client(transport=httpx.MockTransport(_oauth_transport(seen)))
    runtime = LocalPlatformRuntime(http_client=http_client, postgres_store=None)
    runtime.mcp_plugin._client = http_client
    client = TestClient(create_app(runtime))
    client.__enter__()
    try:
        start = client.post(
            "/api/v0/oauth/mcp/start",
            json={
                "endpoint_url": "https://tools.example.com/mcp",
                "display_name": "Docs MCP",
                "connector_id": "custom-mcp",
            },
        )
        assert start.status_code == 200
        authorization = urlsplit(start.json()["authorization_url"])
        assert authorization.hostname == "auth.example.com"
        state = parse_qs(authorization.query)["state"][0]
        assert parse_qs(authorization.query)["code_challenge_method"] == ["S256"]

        callback = client.get(
            "/api/v0/oauth/mcp/callback",
            params={"state": state, "code": "one-time-code"},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        setup_id = parse_qs(urlsplit(callback.headers["location"]).query)[
            "connector_setup"
        ][0]
        setup = client.get(f"/api/v0/oauth/setup/{setup_id}")
        assert setup.status_code == 200
        assert [item["name"] for item in setup.json()["tools"]] == ["search_docs"]

        completed = client.post(
            f"/api/v0/oauth/setup/{setup_id}/complete",
            json={"resources": ["search_docs"]},
        )
        assert completed.status_code == 200
        installation = completed.json()["installation"]
        assert installation["configuration"]["auth_method"] == "mcp_oauth"
        assert installation["configuration"]["catalog_connector_id"] == "custom-mcp"
        assert "access_token" not in json.dumps(installation)
        stored = runtime.installations.get(installation["plugin_installation_id"])
        assert stored.auth_ref is not None
        assert "oauth-access-secret" in runtime.secret_store.get(stored.auth_ref)
        assert any(item.get("method") == "initialize" for item in seen)
        assert any(item.get("method") == "tools/list" for item in seen)

        replay = client.get(
            "/api/v0/oauth/mcp/callback",
            params={"state": state, "code": "one-time-code"},
            follow_redirects=False,
        )
        assert "connector_error=state_invalid" in replay.headers["location"]
    finally:
        client.close()
        runtime.close()


def test_mcp_oauth_rejects_connector_id_endpoint_mismatch(monkeypatch) -> None:
    """Prevent catalog IDs from being attached to arbitrary MCP endpoints."""
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "tools.example.com")
    monkeypatch.setenv("WEAVES_PUBLIC_API_BASE_URL", "https://api.weaves.test")
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    client.__enter__()
    try:
        response = client.post(
            "/api/v0/oauth/mcp/start",
            json={
                "endpoint_url": "https://tools.example.com/mcp",
                "display_name": "Impersonated Datadog",
                "connector_id": "datadog-mcp",
            },
        )
        assert response.status_code == 422
    finally:
        client.close()
        runtime.close()


def test_catalog_exposes_only_configured_mcp_auth_methods(monkeypatch) -> None:
    """Bearer custom MCP remains usable without OAuth callback configuration."""
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "mcp.datadoghq.com")
    monkeypatch.delenv("WEAVES_PUBLIC_API_BASE_URL", raising=False)
    entries = {item["connector_id"]: item for item in connector_catalog({})}
    assert entries["custom-mcp"]["available"] is True
    assert entries["custom-mcp"]["auth_methods"] == ["bearer"]
    assert entries["datadog-mcp"]["available"] is False
    assert entries["datadog-mcp"]["auth_methods"] == ["oauth"]
    assert entries["notion-mcp"]["available"] is False


def test_old_provider_token_install_flow_is_rejected() -> None:
    """Native apps cannot bypass their OAuth authorization flow with raw tokens."""
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    client.__enter__()
    try:
        response = client.post(
            "/api/v0/plugin-installations",
            json={
                "plugin_id": "github",
                "display_name": "Old token setup",
                "api_token": "personal-access-token",
                "repositories": ["owner/repo"],
            },
        )
        assert response.status_code == 422
        assert client.get("/api/v0/oauth/providers").status_code == 404
    finally:
        client.close()
        runtime.close()


def test_expired_mcp_oauth_token_refreshes_only_through_allowlisted_host(
    monkeypatch,
) -> None:
    """Refresh MCP tokens and reject token URLs outside the allowlist."""
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "auth.example.com")
    store = _MemorySecretStore(
        {
            "mcp-token": json.dumps(
                {
                    "weaves_oauth": True,
                    "provider": "mcp",
                    "access_token": "old-token",
                    "refresh_token": "refresh-value",
                    "client_id": "client-value",
                    "token_url": "https://auth.example.com/token",
                    "resource": "https://tools.example.com/mcp",
                    "expires_at": "2000-01-01T00:00:00+00:00",
                }
            )
        }
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"access_token": "new-token", "expires_in": 1800},
            )
        )
    )
    try:
        resolver = OAuthTokenResolver(store, client)
        assert resolver.resolve_for_plugin("mcp-token", "mcp") == "new-token"
        assert json.loads(store.values["mcp-token"])["access_token"] == "new-token"
        refreshed = json.loads(store.values["mcp-token"])
        refreshed["token_url"] = "https://attacker.example.com/token"
        refreshed["expires_at"] = "2000-01-01T00:00:00+00:00"
        store.values["mcp-token"] = json.dumps(refreshed)
        with pytest.raises(ValueError, match="MCP OAuth refresh host is not allowed"):
            resolver.resolve_for_plugin("mcp-token", "mcp")
    finally:
        client.close()


class _MemorySecretStore:
    """Small secret-store fake used to inspect OAuth refresh updates."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, secret_ref: str) -> str:
        return self.values[secret_ref]

    def replace(self, secret_ref: str, secret: str) -> None:
        self.values[secret_ref] = secret


def _oauth_transport(seen: list[dict[str, object]]):
    """Serve OAuth metadata and MCP JSON-RPC responses without network access."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.host == "tools.example.com":
            return httpx.Response(
                200, json={"authorization_servers": ["https://auth.example.com"]}
            )
        if request.method == "GET" and request.url.host == "auth.example.com":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.example.com",
                    "authorization_endpoint": "https://auth.example.com/authorize",
                    "token_endpoint": "https://auth.example.com/token",
                    "registration_endpoint": "https://auth.example.com/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url.path == "/register":
            return httpx.Response(201, json={"client_id": "weaves-test-client"})
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "oauth-access-secret",
                    "refresh_token": "oauth-refresh-secret",
                    "expires_in": 3600,
                },
            )
        if request.url.host == "tools.example.com":
            message = json.loads(request.read())
            seen.append(message)
            if message["method"] == "notifications/initialized":
                return httpx.Response(202)
            result = (
                {"protocolVersion": "2026-07-28", "capabilities": {}}
                if message["method"] == "initialize"
                else {"resultType": "complete", "tools": [_TOOL]}
            )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": message["id"], "result": result},
                headers={"Mcp-Session-Id": "session-test"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handle
