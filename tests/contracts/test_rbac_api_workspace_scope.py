"""Workspace-aware permission response and Builder authorization tests."""

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.contracts.test_rbac_api import (
    _create_rbac_api_scenario,
    _replace_user_with_builder,
)
from tests.support.runtime import LocalPlatformRuntime


def test_builder_can_manage_definitions_but_not_org_admin_settings():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        scenario = _create_rbac_api_scenario(client)
        _replace_user_with_builder(client, scenario)
        builder_headers = scenario.user_headers
        permissions = client.get("/api/v0/auth/me", headers=builder_headers).json()[
            "effective_permissions"
        ]
        assert "agents.manage" in permissions
        assert "workflows.manage" in permissions
        assert "models.manage" not in permissions
        workflow = client.post(
            "/api/v0/workflows",
            headers=builder_headers,
            json={"name": "Builder workflow", "agent_id": scenario.agent_id},
        )
        assert workflow.status_code == 201, workflow.text
        denied = (
            client.get("/api/v0/users", headers=builder_headers),
            client.get("/api/v0/roles", headers=builder_headers),
            client.get("/api/v0/audit-events", headers=builder_headers),
            client.post(
                "/api/v0/model-providers",
                headers=builder_headers,
                json={
                    "provider_type": "openai",
                    "display_name": "Denied provider",
                    "api_key": "not-a-real-key",
                },
            ),
            client.post(
                "/api/v0/plugin-installations",
                headers=builder_headers,
                json={
                    "plugin_id": "mcp",
                    "display_name": "Denied connector",
                    "endpoint_url": "https://tools.example.com/mcp",
                    "bearer_token": "not-a-real-token",
                    "read_only_tool_names": ["search_issues"],
                    "catalog_connector_id": "custom-mcp",
                },
            ),
        )
        assert [response.status_code for response in denied] == [403] * len(denied)


def test_auth_me_resolves_permissions_for_the_selected_workspace():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        scenario = _create_rbac_api_scenario(client)
        second_workspace = client.post(
            "/api/v0/workspaces",
            headers=scenario.owner_headers,
            json={"name": "Second RBAC workspace"},
        )
        assert second_workspace.status_code == 201, second_workspace.text
        second_workspace_id = second_workspace.json()["id"]
        roles = client.get("/api/v0/roles", headers=scenario.owner_headers).json()
        builder_role_id = next(
            role["id"] for role in roles if role["system_role_key"] == "builder"
        )
        assigned = client.put(
            "/api/v0/role-bindings",
            headers={
                **scenario.owner_headers,
                "X-Workspace-ID": second_workspace_id,
            },
            json={
                "principal_id": scenario.user_principal_id,
                "role_id": builder_role_id,
                "workspace_id": second_workspace_id,
            },
        )
        assert assigned.status_code == 200, assigned.text
        first_scope = client.get(
            "/api/v0/auth/me", headers=scenario.user_headers
        ).json()
        second_scope = client.get(
            "/api/v0/auth/me",
            headers={
                **scenario.user_headers,
                "X-Workspace-ID": second_workspace_id,
            },
        )
        assert second_scope.status_code == 200, second_scope.text
        assert "agents.manage" not in first_scope["effective_permissions"]
        assert "agents.manage" in second_scope.json()["effective_permissions"]
        assert second_scope.json()["workspace_id"] == second_workspace_id
