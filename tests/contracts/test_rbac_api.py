"""HTTP authorization tests for the v0 user-level RBAC contract."""

from dataclasses import dataclass

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


@dataclass(frozen=True)
class RbacApiScenario:
    """Persisted owner, user, agent, role, and request scopes for API checks."""

    owner_headers: dict[str, str]
    user_headers: dict[str, str]
    workspace_id: str
    user_principal_id: str
    agent_id: str
    workflow_id: str
    provider_id: str
    profile_id: str
    installation_id: str
    builder_role_id: str


def _create_rbac_api_scenario(client: TestClient) -> RbacApiScenario:
    """Create an owner, test data, and a user assigned the User preset."""
    response = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "RBAC test organization",
            "owner_email": "rbac-owner@example.test",
            "owner_display_name": "RBAC owner",
        },
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["workspace"]["id"]
    owner_headers = {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Workspace-ID": workspace_id,
    }
    roles = client.get("/api/v0/roles", headers=owner_headers).json()
    user_role = next(item for item in roles if item["system_role_key"] == "user")
    builder_role = next(item for item in roles if item["system_role_key"] == "builder")
    agent = client.post(
        "/api/v0/agents",
        headers=owner_headers,
        json={"name": "RBAC agent", "instructions": "Respond briefly."},
    )
    assert agent.status_code == 201, agent.text
    workflow = client.post(
        "/api/v0/workflows",
        headers=owner_headers,
        json={"name": "RBAC workflow", "agent_id": agent.json()["id"]},
    )
    assert workflow.status_code == 201, workflow.text
    _create_sensitive_test_resources(client, owner_headers)
    provider = client.get("/api/v0/model-providers", headers=owner_headers).json()[0]
    profile = client.get("/api/v0/model-profiles", headers=owner_headers).json()[0]
    installation = client.get(
        "/api/v0/plugin-installations", headers=owner_headers
    ).json()[0]
    user = client.post(
        "/api/v0/users",
        headers=owner_headers,
        json={
            "email": "rbac-user@example.test",
            "display_name": "RBAC user",
            "role_id": user_role["id"],
        },
    )
    assert user.status_code == 201, user.text
    user_headers = {
        "Authorization": f"Bearer {user.json()['access_token']}",
        "X-Workspace-ID": workspace_id,
    }
    return RbacApiScenario(
        owner_headers=owner_headers,
        user_headers=user_headers,
        workspace_id=workspace_id,
        user_principal_id=user.json()["principal"]["id"],
        agent_id=agent.json()["id"],
        workflow_id=workflow.json()["definition"]["workflow_id"],
        provider_id=provider["id"],
        profile_id=profile["id"],
        installation_id=installation["plugin_installation_id"],
        builder_role_id=builder_role["id"],
    )


def _create_sensitive_test_resources(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    """Create test secrets so list endpoints can prove they redact them."""
    provider = client.post(
        "/api/v0/model-providers",
        headers=owner_headers,
        json={
            "provider_type": "openai",
            "display_name": "Test provider",
            "api_key": "rbac-provider-secret",
            "initial_model": "test-model-v0",
        },
    )
    assert provider.status_code == 201, provider.text
    connector = client.post(
        "/api/v0/plugin-installations",
        headers=owner_headers,
        json={
            "plugin_id": "slack",
            "display_name": "RBAC test Slack",
            "user_token": "rbac-slack-secret",
            "channels": ["support"],
        },
    )
    assert connector.status_code == 201, connector.text


def _assert_user_can_view_metadata(
    client: TestClient, scenario: RbacApiScenario
) -> None:
    """Check User browse access and ensure listings do not disclose secrets."""
    headers = scenario.user_headers
    assert client.get("/api/v0/agents", headers=headers).status_code == 200
    assert client.get("/api/v0/agent-templates", headers=headers).status_code == 200
    assert client.get("/api/v0/agent-skills", headers=headers).status_code == 200
    agent = client.get(f"/api/v0/agents/{scenario.agent_id}", headers=headers)
    assert agent.status_code == 200
    assert client.get("/api/v0/workflows", headers=headers).status_code == 200
    workflow = client.get(f"/api/v0/workflows/{scenario.workflow_id}", headers=headers)
    assert workflow.status_code == 200
    versions = client.get(
        f"/api/v0/workflows/{scenario.workflow_id}/versions", headers=headers
    )
    assert versions.status_code == 200
    version_id = versions.json()["items"][0]["workflow_version_id"]
    assert (
        client.get(
            f"/api/v0/workflows/{scenario.workflow_id}/versions/{version_id}",
            headers=headers,
        ).status_code
        == 200
    )
    assert client.get("/api/v0/model-profiles", headers=headers).status_code == 200
    providers = client.get("/api/v0/model-providers", headers=headers)
    assert providers.status_code == 200
    assert all("auth_ref" not in item for item in providers.json())
    assert "rbac-provider-secret" not in providers.text
    assert client.get("/api/v0/plugin-descriptors", headers=headers).status_code == 200
    installations = client.get("/api/v0/plugin-installations", headers=headers)
    assert installations.status_code == 200
    assert all("auth_ref" not in item for item in installations.json())
    assert "rbac-slack-secret" not in installations.text
    documents = client.get("/api/v0/knowledge-documents", headers=headers)
    assert documents.status_code == 200
    assert all("text" not in item for item in documents.json())


def _resource_counts(client: TestClient, headers: dict[str, str]) -> tuple[int, ...]:
    """Capture mutable collection sizes before attempting forbidden requests."""
    paths = (
        "/api/v0/agents",
        "/api/v0/workflows",
        "/api/v0/plugin-installations",
        "/api/v0/knowledge-documents",
    )
    return tuple(len(client.get(path, headers=headers).json()) for path in paths)


def _assert_user_mutations_denied(
    client: TestClient, scenario: RbacApiScenario
) -> None:
    """Assert every management category rejects User without data changes."""
    user_headers = scenario.user_headers
    owner_headers = scenario.owner_headers
    before = _resource_counts(client, owner_headers)
    responses = (
        client.post(
            "/api/v0/agents",
            headers=user_headers,
            json={"name": "Denied agent", "instructions": "No changes."},
        ),
        client.post(
            "/api/v0/workflows",
            headers=user_headers,
            json={"name": "Denied workflow", "agent_id": scenario.agent_id},
        ),
        client.post(
            "/api/v0/model-providers",
            headers=user_headers,
            json={
                "provider_type": "openai",
                "display_name": "Denied provider",
                "api_key": "not-a-real-key",
            },
        ),
        client.post(
            "/api/v0/plugin-installations",
            headers=user_headers,
            json={
                "plugin_id": "github",
                "display_name": "Denied connector",
                "api_token": "not-a-real-token",
                "repositories": ["org/repo"],
            },
        ),
        client.post(
            "/api/v0/knowledge-documents",
            headers=user_headers,
            json={"title": "Denied document", "text": "Should not persist."},
        ),
        client.patch(
            f"/api/v0/agents/{scenario.agent_id}",
            headers=user_headers,
            json={"description": "Denied update."},
        ),
        client.patch(
            f"/api/v0/workflows/{scenario.workflow_id}",
            headers=user_headers,
            json={"description": "Denied update."},
        ),
        client.patch(
            f"/api/v0/model-profiles/{scenario.profile_id}",
            headers=user_headers,
            json={"max_output_tokens": 128},
        ),
        client.put(
            f"/api/v0/model-providers/{scenario.provider_id}/credential",
            headers=user_headers,
            json={"api_key": "denied-provider-secret"},
        ),
        client.put(
            f"/api/v0/plugin-installations/{scenario.installation_id}/credential",
            headers=user_headers,
            json={"api_key": "denied-connector-secret"},
        ),
        client.patch(
            f"/api/v0/plugin-installations/{scenario.installation_id}",
            headers=user_headers,
            json={"enabled_capability_ids": []},
        ),
        client.post(
            f"/api/v0/agents/{scenario.agent_id}/archive", headers=user_headers
        ),
    )
    assert [response.status_code for response in responses] == [403] * len(responses)
    assert _resource_counts(client, owner_headers) == before


def _replace_user_with_builder(client: TestClient, scenario: RbacApiScenario) -> None:
    """Replace the User preset and verify Builder receives configuration access."""
    response = client.put(
        "/api/v0/role-bindings",
        headers=scenario.owner_headers,
        json={
            "principal_id": scenario.user_principal_id,
            "role_id": scenario.builder_role_id,
            "workspace_id": scenario.workspace_id,
        },
    )
    assert response.status_code == 200, response.text
    bindings = client.get("/api/v0/role-bindings", headers=scenario.owner_headers)
    assigned = [
        item
        for item in bindings.json()
        if item["principal_id"] == scenario.user_principal_id
        and item["workspace_id"] == scenario.workspace_id
    ]
    assert [item["role_id"] for item in assigned] == [scenario.builder_role_id]
    created = client.post(
        "/api/v0/agents",
        headers=scenario.user_headers,
        json={"name": "Builder agent", "instructions": "Respond briefly."},
    )
    assert created.status_code == 201, created.text


def test_user_can_browse_and_run_but_cannot_mutate_workspace_configuration():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        scenario = _create_rbac_api_scenario(client)
        identity = client.get("/api/v0/auth/me", headers=scenario.user_headers)
        assert identity.status_code == 200
        assert identity.json()["workspace_id"] == scenario.workspace_id
        assert "agents.view" in identity.json()["effective_permissions"]
        assert "agents.manage" not in identity.json()["effective_permissions"]
        _assert_user_can_view_metadata(client, scenario)
        result = client.post(
            "/api/v0/runs",
            headers=scenario.user_headers,
            json={"agent_id": scenario.agent_id, "task": "Say hello."},
        )
        assert result.status_code == 201, result.text
        agent_runs = client.get(
            f"/api/v0/agents/{scenario.agent_id}/runs",
            headers=scenario.user_headers,
        )
        assert agent_runs.status_code == 200, agent_runs.text
        assert agent_runs.json()[0]["run_id"] == result.json()["run"]["run_id"]
        workflow_result = client.post(
            f"/api/v0/workflows/{scenario.workflow_id}/runs",
            headers=scenario.user_headers,
            json={"task": "Say hello through the workflow."},
        )
        assert workflow_result.status_code == 201, workflow_result.text
        assert (
            client.get(
                f"/api/v0/workflows/{scenario.workflow_id}/runs",
                headers=scenario.user_headers,
            ).status_code
            == 200
        )


def test_user_mutations_are_denied_and_builder_replacement_is_atomic():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))
    with client:
        scenario = _create_rbac_api_scenario(client)
        _assert_user_mutations_denied(client, scenario)
        _replace_user_with_builder(client, scenario)


def _assert_custom_permission_paths_are_admin_only(
    client: TestClient, owner_headers: dict[str, str], workspace_id: str
) -> None:
    """Prevent workspace role managers from creating arbitrary permission sets."""
    custom_manager_role = client.post(
        "/api/v0/roles",
        headers=owner_headers,
        json={
            "name": "Workspace user manager",
            "permissions": ["users.manage", "roles.manage"],
        },
    )
    assert custom_manager_role.status_code == 201, custom_manager_role.text
    workspace_admin = client.post(
        "/api/v0/users",
        headers=owner_headers,
        json={
            "email": "workspace-manager@example.test",
            "display_name": "Workspace manager",
            "role_id": custom_manager_role.json()["id"],
        },
    )
    assert workspace_admin.status_code == 201, workspace_admin.text
    delegated_headers = {
        "Authorization": f"Bearer {workspace_admin.json()['access_token']}",
        "X-Workspace-ID": workspace_id,
    }
    before_users = client.get("/api/v0/users", headers=owner_headers).json()
    rejected_user = client.post(
        "/api/v0/users",
        headers=delegated_headers,
        json={
            "email": "arbitrary-role@example.test",
            "display_name": "Arbitrary role",
            "permissions": ["agents.manage"],
        },
    )
    rejected_invitation = client.post(
        "/api/v0/user-invitations",
        headers=delegated_headers,
        json={
            "email": "arbitrary-invite@example.test",
            "display_name": "Arbitrary invitation",
            "permissions": ["agents.manage"],
        },
    )
    assert rejected_user.status_code == 403
    assert rejected_invitation.status_code == 403
    assert client.get("/api/v0/users", headers=owner_headers).json() == before_users


def test_system_role_assignment_scope_is_enforced():
    runtime = LocalPlatformRuntime(postgres_store=None)
    client = TestClient(create_app(runtime))

    with client:
        onboarded = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Scope test organization",
                "owner_email": "scope-owner@example.test",
                "owner_display_name": "Scope owner",
            },
        )
        assert onboarded.status_code == 201, onboarded.text
        headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}
        roles = client.get("/api/v0/roles", headers=headers).json()
        admin_role = next(item for item in roles if item["system_role_key"] == "admin")
        user_role = next(item for item in roles if item["system_role_key"] == "user")
        scoped_headers = {
            **headers,
            "X-Workspace-ID": onboarded.json()["workspace"]["id"],
        }
        current_bindings = client.get("/api/v0/role-bindings", headers=scoped_headers)
        owner_principal_id = next(
            item["principal_id"]
            for item in current_bindings.json()
            if item["role_id"] == admin_role["id"]
        )
        limited_admin = client.post(
            "/api/v0/roles",
            headers=scoped_headers,
            json={"name": "Role manager only", "permissions": ["roles.manage"]},
        )
        assert limited_admin.status_code == 201, limited_admin.text
        last_admin_replacement = client.put(
            "/api/v0/role-bindings",
            headers=scoped_headers,
            json={
                "principal_id": owner_principal_id,
                "role_id": limited_admin.json()["id"],
            },
        )
        assert last_admin_replacement.status_code == 422
        user = client.post(
            "/api/v0/users",
            headers={**headers, "X-Workspace-ID": onboarded.json()["workspace"]["id"]},
            json={
                "email": "scope-user@example.test",
                "display_name": "Scope user",
                "role_id": user_role["id"],
            },
        )
        assert user.status_code == 201, user.text
        _assert_custom_permission_paths_are_admin_only(
            client, scoped_headers, onboarded.json()["workspace"]["id"]
        )
        invalid_assignment = client.put(
            "/api/v0/role-bindings",
            headers=headers,
            json={
                "principal_id": user.json()["principal"]["id"],
                "role_id": admin_role["id"],
                "workspace_id": onboarded.json()["workspace"]["id"],
            },
        )
        assert invalid_assignment.status_code == 422
