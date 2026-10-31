"""Protocol constants and safe transport helpers for MCP clients."""

import base64
import json
import re
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
CLIENT_INFO = {"name": "weaves", "version": "0.1.0"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOOLS = 100
MAX_PAGES = 5
MAX_RPC_SECONDS = 30.0
_SECRET_TEXT_PATTERN = re.compile(
    r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]+=*|"
    r"(\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}\]]+"
)


def request_metadata() -> dict[str, Any]:
    """Return the version and client capabilities used in MCP requests."""
    return {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def safe_mcp_name(value: str) -> str:
    """Encode names that cannot safely be sent in an HTTP header."""
    return safe_header_value(value)


def safe_header_value(value: str) -> str:
    """Preserve printable header values and base64-wrap all other content."""
    visible_ascii = all(char == "\t" or "\x20" <= char <= "\x7e" for char in value)
    trimmed = value == value.strip()
    sentinel = value.startswith("=?base64?") and value.endswith("?=")
    if visible_ascii and trimmed and not sentinel:
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def redact_secrets(value: str) -> str:
    """Redact common credential formats from returned MCP text."""
    return _SECRET_TEXT_PATTERN.sub(_redacted_secret, value)


def contains_mcp_header(value: Any) -> bool:
    """Return whether a JSON schema contains an MCP header extension."""
    if isinstance(value, dict):
        return "x-mcp-header" in value or any(
            contains_mcp_header(child) for child in value.values()
        )
    if isinstance(value, list):
        return any(contains_mcp_header(child) for child in value)
    return False


def last_sse_message(value: str) -> dict[str, Any]:
    """Decode the final JSON-RPC message carried in an SSE response."""
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in [*value.splitlines(), ""]:
        if not line:
            if data_lines:
                parsed = json.loads("\n".join(data_lines))
                if isinstance(parsed, dict):
                    messages.append(parsed)
            data_lines = []
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    for message in reversed(messages):
        if "id" in message:
            return message
    raise ValueError("MCP SSE stream contained no JSON-RPC response")


def _redacted_secret(match: re.Match[str]) -> str:
    return f"{match.group(1) or match.group(2)}[REDACTED]"
