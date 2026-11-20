"""Custom connector metadata must not cross organization boundaries."""

import copy
import json

import httpx
from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.contracts.test_mcp_plugin import _TOOL
from tests.support.runtime import LocalPlatformRuntime


def _organization(client, label):
    response = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "Descriptor tenant " + label,
            "owner_email": label + "@descriptor.test",
            "owner_display_name": "Descriptor owner " + label,
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return {
        "Authorization": "Bearer " + payload["access_token"],
        "X-Workspace-ID": payload["workspace"]["id"],
    }


def _mcp_response(request):
    message = json.loads(request.read())
    label = request.url.path.strip("/")
    tool = copy.deepcopy(_TOOL)
    tool["name"] = "search_" + label
    tool["description"] = label.upper() + "_PRIVATE_TOOL_SCHEMA"
    if message["method"] == "notifications/initialized":
        return httpx.Response(202)
    result = (
        {"protocolVersion": "2026-07-28", "capabilities": {}}
        if message["method"] == "initialize"
        else {"resultType": "complete", "tools": [tool]}
    )
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
    )


def test_imported_descriptors_and_capabilities_are_private_to_their_org(monkeypatch):
    monkeypatch.setenv("WEAVES_MCP_ALLOWED_HOSTS", "tools.example.com")
    runtime = LocalPlatformRuntime(postgres_store=None)
    with httpx.Client(transport=httpx.MockTransport(_mcp_response)) as mcp_client:
        runtime.mcp_plugin._client = mcp_client
        with TestClient(create_app(runtime)) as client:
            actors = {
                label: _organization(client, label) for label in ("alpha", "beta")
            }
            installations = {}
            for label, headers in actors.items():
                response = client.post(
                    "/api/v0/plugin-installations",
                    headers=headers,
                    json={
                        "plugin_id": "mcp",
                        "display_name": label.upper() + "_PRIVATE_CONNECTOR",
                        "endpoint_url": "https://tools.example.com/" + label,
                        "read_only_tool_names": ["search_" + label],
                    },
                )
                assert response.status_code == 201, response.text
                installations[label] = response.json()
            for label, foreign in (("alpha", "beta"), ("beta", "alpha")):
                for endpoint in ("plugin-descriptors", "capabilities"):
                    response = client.get("/api/v0/" + endpoint, headers=actors[label])
                    assert response.status_code == 200, response.text
                    assert label.upper() + "_PRIVATE" in response.text
                    assert foreign.upper() + "_PRIVATE" not in response.text
                    assert installations[foreign]["plugin_id"] not in response.text
                descriptors = client.get(
                    "/api/v0/plugin-descriptors", headers=actors[label]
                ).json()
                assert {"github", "slack", "jira"} <= {
                    item["plugin_id"] for item in descriptors
                }
                foreign_id = installations[foreign]["plugin_installation_id"]
                denied = client.patch(
                    "/api/v0/plugin-installations/" + foreign_id,
                    headers=actors[label],
                    json={"status": "disabled"},
                )
                assert denied.status_code == 404, denied.text
                own_id = installations[label]["plugin_installation_id"]
                health = client.post(
                    "/api/v0/plugin-installations/" + own_id + "/healthcheck",
                    headers=actors[label],
                )
                assert health.status_code == 200, health.text
            _assert_conflict_cannot_release_descriptor(
                client, runtime, actors, installations
            )


def _assert_conflict_cannot_release_descriptor(client, runtime, actors, installations):
    own = runtime.installations.get(installations["alpha"]["plugin_installation_id"])
    foreign = runtime.installations.get(installations["beta"]["plugin_installation_id"])
    conflict = foreign.model_copy(
        update={
            "plugin_installation_id": "legacy-conflict-reference",
            "plugin_id": own.plugin_id,
        }
    )
    runtime.installations.create(conflict)
    before = runtime.installations.list()
    for label, target in (("alpha", own), ("beta", conflict)):
        response = client.get("/api/v0/plugin-descriptors", headers=actors[label])
        assert "ALPHA_PRIVATE" not in response.text
        path = "/api/v0/plugin-installations/" + target.plugin_installation_id
        for response in (
            client.delete(path, headers=actors[label]),
            client.patch(path, headers=actors[label], json={"status": "disabled"}),
            client.post(path + "/healthcheck", headers=actors[label]),
        ):
            assert response.status_code == 404, response.text
    assert runtime.installations.list() == before
