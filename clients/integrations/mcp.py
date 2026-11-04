"""Read-only MCP Streamable HTTP client used behind the plugin gateway."""

import hashlib
import json
import os
import re
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from clients.integrations.mcp_protocol import (
    MAX_PAGES,
    MAX_TOOLS,
    contains_mcp_header,
    redact_secrets,
    request_metadata,
)
from clients.integrations.mcp_session import McpSessionMixin
from contracts.v1 import (
    CapabilityKind,
    CapabilitySpec,
    PluginInstallation,
)
from contracts.v1.json_data import thaw_json_value
from shared.errors import PluginGatewayError


class SecretResolver(Protocol):
    def resolve(self, auth_ref: str) -> str: ...


class McpHttpPlugin(McpSessionMixin):
    """MCP client that exposes only explicitly allowlisted search tools."""

    def __init__(self, secrets: SecretResolver, client: httpx.Client) -> None:
        self._secrets = secrets
        self._client = client

    def validate_endpoint(self, endpoint: str) -> str:
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise PluginGatewayError(
                "mcp.endpoint_invalid", "MCP endpoint URL is invalid"
            ) from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PluginGatewayError(
                "mcp.endpoint_invalid",
                "MCP endpoints must be HTTPS URLs without credentials or query strings",
            )
        allowed_hosts = {
            host.strip().casefold()
            for host in os.environ.get("WEAVES_MCP_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        }
        if parsed.hostname.casefold() not in allowed_hosts:
            raise PluginGatewayError(
                "mcp.host_not_allowed",
                "MCP endpoint host is not in WEAVES_MCP_ALLOWED_HOSTS",
            )
        if port not in (None, 443):
            raise PluginGatewayError(
                "mcp.port_not_allowed", "MCP endpoints must use HTTPS port 443"
            )
        return endpoint.rstrip("/")

    def discover_tools(
        self,
        endpoint: str,
        bearer_token: str | None = None,
        *,
        initialize: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        endpoint = self.validate_endpoint(endpoint)
        session_id = self._initialize(endpoint, bearer_token) if initialize else None
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"_meta": request_metadata()}
            if cursor:
                params["cursor"] = cursor
            result, session_id = self._rpc_with_session(
                endpoint,
                method="tools/list",
                params=params,
                bearer_token=bearer_token,
                session_id=session_id,
            )
            if result.get("resultType") != "complete":
                raise PluginGatewayError(
                    "mcp.result_incomplete",
                    "MCP server returned an incomplete tool list",
                )
            page = result.get("tools")
            if not isinstance(page, list):
                raise PluginGatewayError(
                    "mcp.tools_invalid", "MCP server returned an invalid tool list"
                )
            tools.extend(tool for tool in page if isinstance(tool, dict))
            if len(tools) > MAX_TOOLS:
                raise PluginGatewayError(
                    "mcp.tool_limit_exceeded", "MCP server exposes too many tools"
                )
            cursor = result.get("nextCursor")
            if not cursor:
                names = [tool.get("name") for tool in tools]
                if any(
                    not isinstance(name, str) or not name or len(name) > 128
                    for name in names
                ):
                    raise PluginGatewayError(
                        "mcp.tools_invalid", "MCP server returned an invalid tool name"
                    )
                if len(names) != len(set(names)):
                    raise PluginGatewayError(
                        "mcp.tools_invalid", "MCP server returned duplicate tool names"
                    )
                return tuple(tools)
        raise PluginGatewayError(
            "mcp.pagination_limit", "MCP tool discovery exceeded its page limit"
        )

    def validate_selected_tools(
        self, tools: tuple[dict[str, Any], ...], selected_names: tuple[str, ...]
    ) -> tuple[dict[str, Any], ...]:
        if not selected_names or len(selected_names) > MAX_TOOLS:
            raise ValueError("select between 1 and 100 read-only MCP tools")
        if len(set(selected_names)) != len(selected_names):
            raise ValueError("MCP tool allowlist must contain unique names")
        available: dict[str, dict[str, Any]] = {}
        for tool in tools:
            name = tool.get("name")
            if isinstance(name, str):
                available[name] = tool
        if not set(selected_names).issubset(available):
            raise ValueError("allowlisted MCP tools must exist on the MCP server")
        selected = tuple(available[name] for name in selected_names)
        for tool in selected:
            if not is_query_tool(tool):
                raise ValueError(
                    "MCP read tools must require only a string query parameter"
                )
        return selected

    def healthcheck(self, installation: PluginInstallation) -> dict[str, str]:
        config = thaw_json_value(installation.configuration)
        tools = self.discover_tools(
            config["endpoint"], self._resolve_token(installation), initialize=True
        )
        available = {tool.get("name"): tool for tool in tools}
        selected = config.get("tools", {}).values()
        for entry in selected:
            if not isinstance(entry, dict):
                raise PluginGatewayError(
                    "mcp.configuration_invalid", "MCP tool mapping is invalid"
                )
            remote = available.get(entry.get("name"))
            if remote is None or schema_fingerprint(remote) != entry.get(
                "input_schema_hash"
            ):
                raise PluginGatewayError(
                    "mcp.tools_changed",
                    "One or more allowlisted MCP tools have changed",
                )
        return {
            "server": "reachable",
            "available_tools": str(len(tools)),
            "allowlisted_tools": str(len(config.get("tools", {}))),
        }

    def invoke_for_installation(
        self,
        capability_id: str,
        payload: dict[str, Any],
        installation: PluginInstallation,
    ) -> dict[str, Any]:
        config = thaw_json_value(installation.configuration)
        tool_binding = config.get("tools", {}).get(capability_id)
        tool_name: str | None
        query_header: str | None
        if isinstance(tool_binding, str):
            tool_name = tool_binding
            query_header = None
        elif isinstance(tool_binding, dict):
            tool_name = tool_binding.get("name")
            query_header = tool_binding.get("query_header")
        else:
            tool_name = None
            query_header = None
        if not isinstance(tool_name, str):
            raise PluginGatewayError(
                "mcp.capability_not_mapped", "MCP capability is not mapped"
            )
        if query_header and len(str(payload.get("query", ""))) > 2048:
            raise PluginGatewayError(
                "mcp.query_too_long", "MCP query exceeds the header transport limit"
            )
        token = self._resolve_token(installation)
        session_id = self._initialize(config["endpoint"], token)
        result = self._rpc(
            config["endpoint"],
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": payload,
                "_meta": request_metadata(),
            },
            bearer_token=token,
            name=tool_name,
            session_id=session_id,
            parameter_headers=(
                {query_header: payload["query"]} if query_header else None
            ),
        )
        if result.get("resultType") != "complete" or result.get("isError", False):
            raise PluginGatewayError(
                "mcp.tool_call_failed", "MCP tool did not return a completed result"
            )
        text_parts = [
            item["text"]
            for item in result.get("content", [])
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        structured = result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            rendered = json.dumps(structured, ensure_ascii=False, allow_nan=False)
            if rendered:
                text_parts.append(rendered)
        text = redact_secrets("\n".join(text_parts))[:2000]
        return {
            "documents": [
                {
                    "source_id": (
                        f"mcp:{installation.plugin_installation_id}:{capability_id}"
                    ),
                    "title": tool_name[:160],
                    "text": text or "MCP tool returned no textual context.",
                }
            ]
        }

    def _resolve_token(self, installation: PluginInstallation) -> str | None:
        if not installation.auth_ref:
            return None
        try:
            resolver = getattr(self._secrets, "resolve_for_plugin", None)
            token = (
                resolver(installation.auth_ref, "mcp")
                if callable(resolver)
                else self._secrets.resolve(installation.auth_ref)
            )
        except Exception as exc:
            raise PluginGatewayError(
                "mcp.credential_unavailable", "MCP credential is unavailable"
            ) from exc
        return token


def imported_capability(capability_id: str, tool: dict[str, Any]) -> CapabilitySpec:
    schema = tool["inputSchema"]
    return CapabilitySpec(
        capability_id=capability_id,
        name=str(tool.get("title") or tool["name"])[:160],
        description=str(tool.get("description", ""))[:1000],
        kind=CapabilityKind.CONTEXT_READ,
        input_schema=schema,
        output_schema={
            "type": "object",
            "properties": {
                "documents": {
                    "type": "array",
                    "maxItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {"type": "string", "maxLength": 128},
                            "title": {"type": "string", "maxLength": 160},
                            "text": {"type": "string", "maxLength": 2000},
                        },
                        "required": ["source_id", "title", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["documents"],
            "additionalProperties": False,
        },
        timeout_seconds=30,
    )


def capability_id_for_tool(tool_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", tool_name.casefold()).strip("-")[:40]
    if not slug:
        slug = "tool"
    return f"mcp.{slug}-{uuid4().hex[:8]}"


def is_query_tool(tool: dict[str, Any]) -> bool:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    query_schema = properties.get("query")
    if (
        not isinstance(query_schema, dict)
        or query_schema.get("type") != "string"
        or "query" not in required
        or not all(key == "query" for key in required)
    ):
        return False
    for key, property_schema in properties.items():
        if key != "query" and contains_mcp_header(property_schema):
            return False
    if any(
        contains_mcp_header(value)
        for key, value in query_schema.items()
        if key != "x-mcp-header"
    ):
        return False
    header_name = query_schema.get("x-mcp-header")
    return header_name is None or (
        isinstance(header_name, str)
        and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header_name) is not None
    )


def query_header_for_tool(tool: dict[str, Any]) -> str | None:
    if not is_query_tool(tool):
        return None
    header = tool["inputSchema"]["properties"]["query"].get("x-mcp-header")
    return header if isinstance(header, str) else None


def schema_fingerprint(tool: dict[str, Any]) -> str:
    encoded = json.dumps(
        tool.get("inputSchema"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "McpHttpPlugin",
    "capability_id_for_tool",
    "imported_capability",
    "is_query_tool",
    "query_header_for_tool",
    "schema_fingerprint",
]
