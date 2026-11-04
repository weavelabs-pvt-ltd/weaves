"""Code-owned catalog of supported organizational connectors."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

CURATED_MCP_ENDPOINTS = {
    "notion-mcp": "https://mcp.notion.com/mcp",
    "datadog-mcp": "https://mcp.datadoghq.com/v1/mcp",
}


def connector_catalog(oauth_ready: dict[str, bool]) -> tuple[dict[str, Any], ...]:
    """Return curated connector metadata and environment-specific readiness."""
    allowed_hosts = _allowed_mcp_hosts()
    public_api_ready = bool(os.environ.get("WEAVES_PUBLIC_API_BASE_URL", "").strip())
    entries = (
        _native_entry(
            "github",
            "GitHub",
            "Issues, pull requests, and source code.",
            "Issue & project tracking",
            "github_app",
            oauth_ready.get("github", False),
        ),
        _native_entry(
            "slack",
            "Slack",
            "Search messages in selected channels.",
            "Work apps",
            "slack_oauth_v2",
            oauth_ready.get("slack", False),
        ),
        _native_entry(
            "jira",
            "Jira",
            "Search issues in selected projects.",
            "Issue & project tracking",
            "atlassian_3lo",
            oauth_ready.get("jira", False),
        ),
        _mcp_entry(
            "notion-mcp",
            "Notion",
            "Search and work with an authorized Notion workspace.",
            "Work apps",
            CURATED_MCP_ENDPOINTS["notion-mcp"],
            "oauth",
            allowed_hosts,
            public_api_ready,
        ),
        _mcp_entry(
            "datadog-mcp",
            "Datadog",
            "Explore observability data through Datadog MCP tools.",
            "Observability",
            CURATED_MCP_ENDPOINTS["datadog-mcp"],
            "oauth",
            allowed_hosts,
            public_api_ready,
        ),
        {
            "connector_id": "custom-mcp",
            "display_name": "Custom MCP server",
            "description": "Connect a trusted remote MCP server by URL.",
            "category": "Custom tools",
            "connector_type": "mcp",
            "setup_method": "custom_mcp",
            "endpoint_url": None,
            "auth_methods": ["bearer", "oauth"] if public_api_ready else ["bearer"],
            "available": bool(allowed_hosts),
            "unavailable_reason": _custom_unavailable_reason(allowed_hosts),
            "configured": bool(allowed_hosts),
        },
    )
    return entries


def oauth_provider_readiness() -> dict[str, bool]:
    """Report whether each first-party OAuth connector is configured."""
    public_api_ready = bool(os.environ.get("WEAVES_PUBLIC_API_BASE_URL", "").strip())
    return {
        "github": public_api_ready
        and all(
            os.environ.get(name, "").strip()
            for name in (
                "WEAVES_GITHUB_APP_CLIENT_ID",
                "WEAVES_GITHUB_APP_CLIENT_SECRET",
                "WEAVES_GITHUB_APP_SLUG",
                "WEAVES_GITHUB_APP_ID",
                "WEAVES_GITHUB_APP_PRIVATE_KEY",
            )
        ),
        "slack": public_api_ready
        and all(
            os.environ.get(name, "").strip()
            for name in (
                "WEAVES_SLACK_OAUTH_CLIENT_ID",
                "WEAVES_SLACK_OAUTH_CLIENT_SECRET",
            )
        ),
        "jira": public_api_ready
        and all(
            os.environ.get(name, "").strip()
            for name in (
                "WEAVES_ATLASSIAN_OAUTH_CLIENT_ID",
                "WEAVES_ATLASSIAN_OAUTH_CLIENT_SECRET",
            )
        ),
    }


def _native_entry(
    connector_id: str,
    display_name: str,
    description: str,
    category: str,
    auth_method: str,
    configured: bool,
) -> dict[str, Any]:
    """Build a catalog item backed by a first-party API adapter."""
    return {
        "connector_id": connector_id,
        "display_name": display_name,
        "description": description,
        "category": category,
        "connector_type": "api",
        "setup_method": "oauth",
        "endpoint_url": None,
        "auth_methods": [auth_method],
        "available": configured,
        "configured": configured,
        "unavailable_reason": None if configured else "OAuth app setup is required.",
    }


def _mcp_entry(
    connector_id: str,
    display_name: str,
    description: str,
    category: str,
    endpoint: str,
    auth_method: str,
    allowed_hosts: set[str],
    public_api_ready: bool,
) -> dict[str, Any]:
    """Build a curated MCP entry and report whether its endpoint is allowed."""
    hostname = urlsplit(endpoint).hostname or ""
    host_allowed = hostname.casefold() in allowed_hosts
    needs_callback = auth_method == "oauth"
    configured = host_allowed and (public_api_ready if needs_callback else True)
    reason = None
    if not host_allowed:
        reason = "Ask your administrator to allow this MCP host."
    elif needs_callback and not public_api_ready:
        reason = "Public callback URL is not configured."
    return {
        "connector_id": connector_id,
        "display_name": display_name,
        "description": description,
        "category": category,
        "connector_type": "mcp",
        "setup_method": "curated_mcp",
        "endpoint_url": endpoint,
        "auth_methods": [auth_method],
        "available": configured,
        "configured": configured,
        "unavailable_reason": reason,
    }


def _allowed_mcp_hosts() -> set[str]:
    """Read the exact MCP host allowlist without accepting URL fragments."""
    return {
        host.strip().casefold()
        for host in os.environ.get("WEAVES_MCP_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }


def _custom_unavailable_reason(allowed_hosts: set[str]) -> str | None:
    """Describe the missing deployment configuration for custom MCP setup."""
    if not allowed_hosts:
        return "Ask your administrator to configure the MCP host allowlist."
    return None


__all__ = [
    "CURATED_MCP_ENDPOINTS",
    "connector_catalog",
    "oauth_provider_readiness",
]
