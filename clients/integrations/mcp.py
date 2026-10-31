"""Read-only MCP Streamable HTTP client used behind the plugin gateway."""

import hashlib
import json
import os
import re
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from clients.integrations.mcp_protocol import (
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    MAX_RPC_SECONDS,
    MAX_TOOLS,
    PROTOCOL_VERSION,
    contains_mcp_header,
    last_sse_message,
    redact_secrets,
    request_metadata,
    safe_header_value,
    safe_mcp_name,
)
from contracts.v1 import (
    CapabilityKind,
    CapabilitySpec,
    PluginInstallation,
)
from contracts.v1.json_data import thaw_json_value
from shared.errors import PluginGatewayError


class SecretResolver(Protocol):
    def resolve(self, auth_ref: str) -> str: ...


class McpHttpPlugin:
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
        self, endpoint: str, bearer_token: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        endpoint = self.validate_endpoint(endpoint)
        cursor: str | None = None
        tools: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"_meta": request_metadata()}
            if cursor:
                params["cursor"] = cursor
            result = self._rpc(
                endpoint,
                method="tools/list",
                params=params,
                bearer_token=bearer_token,
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
            config["endpoint"], self._resolve_token(installation)
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
        result = self._rpc(
            config["endpoint"],
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": payload,
                "_meta": request_metadata(),
            },
            bearer_token=self._resolve_token(installation),
            name=tool_name,
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
            token = self._secrets.resolve(installation.auth_ref)
        except Exception as exc:
            raise PluginGatewayError(
                "mcp.credential_unavailable", "MCP credential is unavailable"
            ) from exc
        return token

    def _rpc(
        self,
        endpoint: str,
        *,
        method: str,
        params: dict[str, Any],
        bearer_token: str | None = None,
        name: str | None = None,
        parameter_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        endpoint = self.validate_endpoint(endpoint)
        request_id = str(uuid4())
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        headers = self._rpc_headers(method, name, bearer_token, parameter_headers)
        body, content_type = self._send_rpc(endpoint, message, headers)
        return _parse_rpc_result(body, content_type, request_id)

    def _rpc_headers(
        self,
        method: str,
        name: str | None,
        bearer_token: str | None,
        parameter_headers: dict[str, str] | None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name:
            headers["Mcp-Name"] = safe_mcp_name(name)
        for key, value in (parameter_headers or {}).items():
            headers[f"Mcp-Param-{key}"] = safe_header_value(value)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        return headers

    def _send_rpc(
        self, endpoint: str, message: dict[str, Any], headers: dict[str, str]
    ) -> tuple[bytearray, str]:
        deadline = monotonic() + MAX_RPC_SECONDS
        timeout = httpx.Timeout(
            MAX_RPC_SECONDS, connect=5.0, read=5.0, write=5.0, pool=5.0
        )
        try:
            with self._client.stream(
                "POST", endpoint, json=message, headers=headers, timeout=timeout
            ) as response:
                if response.is_error:
                    raise PluginGatewayError(
                        "mcp.http_error", "MCP server returned an HTTP error"
                    )
                body = _read_rpc_body(response, deadline)
                return body, response.headers.get("content-type", "")
        except PluginGatewayError:
            raise
        except Exception as exc:
            raise PluginGatewayError(
                "mcp.transport_error", "MCP server could not be reached"
            ) from exc


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


def _read_rpc_body(response: httpx.Response, deadline: float) -> bytearray:
    body = bytearray()
    for chunk in response.iter_bytes():
        if monotonic() > deadline:
            raise PluginGatewayError(
                "mcp.timeout", "MCP request exceeded its time limit"
            )
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise PluginGatewayError(
                "mcp.response_too_large", "MCP response exceeded its size limit"
            )
    return body


def _parse_rpc_result(
    body: bytearray, content_type: str, request_id: str
) -> dict[str, Any]:
    try:
        decoded = _decode_rpc_body(body, content_type)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PluginGatewayError(
            "mcp.response_invalid", "MCP server returned an invalid response"
        ) from exc
    if not isinstance(decoded, dict) or decoded.get("id") != request_id:
        raise PluginGatewayError(
            "mcp.response_invalid", "MCP server returned a mismatched response"
        )
    if "error" in decoded:
        raise PluginGatewayError("mcp.rpc_error", "MCP server reported an RPC error")
    result = decoded.get("result")
    if not isinstance(result, dict):
        raise PluginGatewayError(
            "mcp.response_invalid", "MCP server returned an invalid result"
        )
    return result


def _decode_rpc_body(body: bytearray, content_type: str) -> Any:
    if "text/event-stream" in content_type:
        return last_sse_message(body.decode("utf-8"))
    return json.loads(body)


__all__ = [
    "McpHttpPlugin",
    "capability_id_for_tool",
    "imported_capability",
    "is_query_tool",
    "query_header_for_tool",
    "schema_fingerprint",
]
