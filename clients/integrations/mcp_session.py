"""Manage MCP initialization, JSON-RPC sessions, and bounded HTTP responses."""

from __future__ import annotations

import json
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx

from clients.integrations.mcp_protocol import (
    MAX_RESPONSE_BYTES,
    MAX_RPC_SECONDS,
    PROTOCOL_VERSION,
    last_sse_message,
    safe_header_value,
    safe_mcp_name,
)
from shared.errors import PluginGatewayError


class McpSessionMixin:
    """Provide protocol negotiation and transport methods for MCP clients."""

    _client: httpx.Client

    def validate_endpoint(self, endpoint: str) -> str:
        """Validate an MCP URL before the session layer sends requests."""
        raise NotImplementedError

    def _rpc(
        self,
        endpoint: str,
        *,
        method: str,
        params: dict[str, Any],
        bearer_token: str | None = None,
        name: str | None = None,
        parameter_headers: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Send one request and return its validated JSON-RPC result."""
        result, _ = self._rpc_with_session(
            endpoint,
            method=method,
            params=params,
            bearer_token=bearer_token,
            name=name,
            parameter_headers=parameter_headers,
            session_id=session_id,
        )
        return result

    def _rpc_with_session(
        self,
        endpoint: str,
        *,
        method: str,
        params: dict[str, Any],
        bearer_token: str | None = None,
        name: str | None = None,
        parameter_headers: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Send JSON-RPC and retain the server's negotiated session ID."""
        endpoint = self.validate_endpoint(endpoint)
        request_id = str(uuid4())
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        headers = self._rpc_headers(
            method, name, bearer_token, parameter_headers, session_id
        )
        body, content_type, response_session = self._send_rpc(
            endpoint, message, headers
        )
        return (
            _parse_rpc_result(body, content_type, request_id),
            response_session or session_id,
        )

    @staticmethod
    def _rpc_headers(
        method: str,
        name: str | None,
        bearer_token: str | None,
        parameter_headers: dict[str, str] | None,
        session_id: str | None = None,
    ) -> dict[str, str]:
        """Build protocol headers after validating names and values."""
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
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _initialize(self, endpoint: str, bearer_token: str | None) -> str | None:
        """Negotiate the MCP protocol session before listing or calling tools."""
        result, session_id = self._rpc_with_session(
            endpoint,
            method="initialize",
            params={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "weaves", "version": "0.1.0"},
            },
            bearer_token=bearer_token,
        )
        if not isinstance(result.get("protocolVersion"), str):
            raise PluginGatewayError(
                "mcp.initialize_invalid", "MCP server returned invalid initialize data"
            )
        endpoint = self.validate_endpoint(endpoint)
        headers = self._rpc_headers(
            "notifications/initialized", None, bearer_token, None, session_id
        )
        message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._send_rpc(endpoint, message, headers)
        return session_id

    def _send_rpc(
        self, endpoint: str, message: dict[str, Any], headers: dict[str, str]
    ) -> tuple[bytearray, str, str | None]:
        """Send a bounded MCP HTTP request without following redirects."""
        deadline = monotonic() + MAX_RPC_SECONDS
        timeout = httpx.Timeout(
            MAX_RPC_SECONDS, connect=5.0, read=5.0, write=5.0, pool=5.0
        )
        try:
            with self._client.stream(
                "POST",
                endpoint,
                json=message,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if response.is_error:
                    raise PluginGatewayError(
                        "mcp.http_error", "MCP server returned an HTTP error"
                    )
                body = _read_rpc_body(response, deadline)
                return (
                    body,
                    response.headers.get("content-type", ""),
                    response.headers.get("Mcp-Session-Id"),
                )
        except PluginGatewayError:
            raise
        except Exception as exc:
            raise PluginGatewayError(
                "mcp.transport_error", "MCP server could not be reached"
            ) from exc


def _read_rpc_body(response: httpx.Response, deadline: float) -> bytearray:
    """Read an MCP body while enforcing time and response-size limits."""
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
    """Decode and validate one MCP JSON-RPC response envelope."""
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
    """Decode either a JSON response or its final JSON-RPC SSE message."""
    if "text/event-stream" in content_type:
        return last_sse_message(body.decode("utf-8"))
    return json.loads(body)


__all__ = ["McpSessionMixin"]
