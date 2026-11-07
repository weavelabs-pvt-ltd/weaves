import json

import httpx
from fastapi.testclient import TestClient

from apps.api.app import create_app
from clients.integrations.mcp_protocol import last_sse_message, safe_header_value
from tests.support.runtime import LocalPlatformRuntime

_TOOL = {
    "name": "search_docs",
    "title": "Search documentation",
    "description": "Search internal docs using a query.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "x-mcp-header": "Search-Text",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def test_mcp_sse_and_header_values_follow_transport_rules():
    message = last_sse_message(
        ': keepalive\n\nevent: message\ndata: {"jsonrpc":"2.0",\n'
        'data: "id":"abc","result":{}}\n\n'
    )
    assert message["id"] == "abc"
    assert safe_header_value("Hello, 世界") == "=?base64?SGVsbG8sIOS4lueVjA==?="
    assert safe_header_value(" padded ") == "=?base64?IHBhZGRlZCA=?="


def test_mcp_tools_are_allowlisted_governed_and_audited(monkeypatch):
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "tools.example.com")
    requests = []
    active_tool = json.loads(json.dumps(_TOOL))
    respond = _mcp_mock_handler(active_tool, requests)
    runtime = LocalPlatformRuntime()
    mcp_client = httpx.Client(transport=httpx.MockTransport(respond))
    runtime.mcp_plugin._client = mcp_client
    client = TestClient(create_app(runtime))
    try:
        with client:
            installation, imported = _discover_and_install_mcp(client)
            _assert_mcp_tool_runs_and_redacts_secrets(client, runtime, imported)
            _assert_mcp_schema_drift_and_tool_rejection(
                client, active_tool, installation, requests
            )
            _assert_mcp_host_is_allowlisted(client, requests)
    finally:
        client.close()
        mcp_client.close()


def _mcp_mock_handler(active_tool, requests):
    """Create a deterministic MCP transport response handler."""

    def respond(request):
        message = json.loads(request.read())
        requests.append((request, message))
        assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
        assert request.headers["Mcp-Method"] == message["method"]
        assert request.headers["Authorization"] == "Bearer mcp-secret"
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = _mcp_result_for_request(request, message, active_tool)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": result},
            headers={"Mcp-Session-Id": "session-test"},
        )

    return respond


def _mcp_result_for_request(request, message, active_tool):
    """Return a valid MCP handshake, tools/list, or tools/call result."""
    if message["method"] == "initialize":
        return {"protocolVersion": "2026-07-28", "capabilities": {}}
    if message["method"] == "tools/list":
        return {"resultType": "complete", "tools": [active_tool]}
    if message["method"] != "tools/call":
        raise AssertionError(f"unexpected MCP method: {message['method']}")
    assert request.headers["Mcp-Name"] == "search_docs"
    assert request.headers["Mcp-Param-Search-Text"] == "launch checklist"
    assert message["params"]["arguments"]["query"] == "launch checklist"
    return {
        "resultType": "complete",
        "content": [
            {
                "type": "text",
                "text": "Launch checklist facts. api_key=hiddenvalue "
                "Bearer hidden-bearer-token",
            }
        ],
    }


def _discover_and_install_mcp(client):
    """Discover an MCP tool, install it, and return its descriptor and capability."""
    discovery = client.post(
        "/api/v0/mcp/discover",
        json={
            "endpoint_url": "https://tools.example.com/mcp",
            "bearer_token": "mcp-secret",
        },
    )
    assert discovery.status_code == 200
    assert discovery.json()[0]["read_tool_supported"] is True
    response = client.post(
        "/api/v0/plugin-installations",
        json={
            "plugin_id": "mcp",
            "display_name": "Internal docs",
            "endpoint_url": "https://tools.example.com/mcp",
            "bearer_token": "mcp-secret",
            "read_only_tool_names": ["search_docs"],
        },
    )
    assert response.status_code == 201
    installation = response.json()
    assert "auth_ref" not in installation
    healthcheck = client.post(
        f"/api/v0/plugin-installations/{installation['plugin_installation_id']}"
        "/healthcheck"
    )
    assert healthcheck.status_code == 200
    assert healthcheck.json()["allowlisted_tools"] == "1"
    capabilities = client.get("/api/v0/capabilities").json()
    imported = next(
        item
        for item in capabilities
        if item["capability_id"].startswith("mcp.search-docs-")
    )
    assert imported["input_schema"] == _TOOL["inputSchema"]
    return installation, imported


def _assert_mcp_tool_runs_and_redacts_secrets(client, runtime, imported):
    """Execute a Task using an MCP context tool and verify redaction/audit."""
    agent = client.post(
        "/api/v0/agents",
        json={
            "name": "MCP context agent",
            "instructions": "Use approved documentation search.",
            "allowed_capability_ids": [imported["capability_id"]],
        },
    )
    assert agent.status_code == 201
    task = client.post(
        "/api/v0/tasks",
        json={
            "agent_id": agent.json()["id"],
            "title": "Find launch checklist",
            "initial_message": "launch checklist",
        },
    )
    assert task.status_code == 202
    turn = runtime.task_worker_service.process_next_turn("mcp-task-worker")
    assert turn is not None and turn.status.value == "succeeded"
    messages = client.get(f"/api/v0/tasks/{task.json()['task_id']}/messages").json()
    response = next(
        item["content"] for item in messages["items"] if item["role"] == "assistant"
    )
    assert "hiddenvalue" not in response
    assert "hidden-bearer-token" not in response
    events = client.get("/api/v0/audit-events").json()
    assert any(event["action"] == "plugin.capability.succeeded" for event in events)


def _assert_mcp_schema_drift_and_tool_rejection(
    client, active_tool, installation, requests
):
    """Detect changed tool schema and reject a write tool in read-only mode."""
    active_tool["inputSchema"]["properties"]["query"]["maxLength"] = 400
    installation_id = installation["plugin_installation_id"]
    drift = client.post(f"/api/v0/plugin-installations/{installation_id}/healthcheck")
    assert drift.status_code == 502
    installed = [
        item
        for item in client.get("/api/v0/plugin-installations").json()
        if item["plugin_id"] == installation["plugin_id"]
    ]
    assert len(installed) == 1 and installed[0]["status"] == "error"
    rejected = client.post(
        "/api/v0/plugin-installations",
        json={
            "plugin_id": "mcp",
            "display_name": "Invalid MCP",
            "endpoint_url": "https://tools.example.com/mcp",
            "bearer_token": "mcp-secret",
            "read_only_tool_names": ["write_to_prod"],
        },
    )
    assert rejected.status_code == 422
    assert sum(item[1]["method"] == "tools/call" for item in requests) == 1


def _assert_mcp_host_is_allowlisted(client, requests):
    """Reject discovery for a host outside the configured allowlist."""
    request_count = len(requests)
    response = client.post(
        "/api/v0/mcp/discover",
        json={"endpoint_url": "https://untrusted.example/mcp"},
    )
    assert response.status_code == 422
    assert len(requests) == request_count
