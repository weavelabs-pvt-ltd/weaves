from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


@dataclass(frozen=True)
class OrganizationSetup:
    payload: dict[str, Any]
    headers: dict[str, str]
    org_id: str
    workspace_id: str
    principal_id: str


@dataclass(frozen=True)
class WorkspaceSetup:
    workspace_id: str
    headers: dict[str, str]
    agent_id: str


@dataclass(frozen=True)
class MemberSetup:
    user_id: str
    principal_id: str
    headers: dict[str, str]
    session_headers: dict[str, str]


def test_organization_onboarding_scopes_requests_and_runs_agent():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        organization = _onboard_organization(client)
        _verify_owner_access_and_agent_run(client, runtime, organization)
        runner_role_id = _verify_approval_policy_management(
            client, organization.headers, organization.workspace_id
        )
        workspace = _create_secondary_workspace(
            client, organization.headers, organization.workspace_id
        )
        member = _create_workspace_member(client, workspace.headers, runner_role_id)
        _verify_member_scope_restrictions(client, member, workspace)
        _verify_artifact_role_binding(
            client, member, workspace, organization.workspace_id
        )
        _verify_member_suspension_and_reactivation(client, member, workspace)
        _verify_cross_tenant_isolation(client, organization)


def _onboard_organization(client: TestClient) -> OrganizationSetup:
    response = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "Acme Research",
            "owner_email": "Owner@Example.com",
            "owner_display_name": "Acme Owner",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return OrganizationSetup(
        payload=payload,
        headers={"Authorization": f"Bearer {payload['access_token']}"},
        org_id=payload["organization"]["id"],
        workspace_id=payload["workspace"]["id"],
        principal_id=payload["principal"]["id"],
    )


def _verify_owner_access_and_agent_run(
    client: TestClient, runtime: LocalPlatformRuntime, organization: OrganizationSetup
) -> None:
    headers = organization.headers
    organization_list = client.get("/api/v0/organizations", headers=headers)
    assert organization_list.status_code == 200
    assert [item["id"] for item in organization_list.json()] == [organization.org_id]
    workspace_list = client.get("/api/v0/workspaces", headers=headers)
    assert workspace_list.status_code == 200
    assert [item["id"] for item in workspace_list.json()] == [organization.workspace_id]
    installations = client.get("/api/v0/plugin-installations", headers=headers)
    assert installations.status_code == 200
    installation_id = installations.json()[0]["plugin_installation_id"]
    health = client.post(
        f"/api/v0/plugin-installations/{installation_id}/healthcheck",
        headers=headers,
    )
    assert health.status_code == 200
    assert health.json()["documents"] == "1"

    agent = client.post(
        "/api/v0/agents",
        headers=headers,
        json={
            "name": "Research assistant",
            "instructions": "Answer using the workspace knowledge.",
        },
    )
    assert agent.status_code == 201, agent.text
    task = client.post(
        "/api/v0/tasks",
        headers=headers,
        json={
            "title": "Workspace guide",
            "initial_message": "Summarize the workspace guide",
            "agent_id": agent.json()["id"],
        },
    )
    assert task.status_code == 202, task.text
    runtime.task_worker_service.process_next_turn("onboarding-test")
    detail = client.get(f"/api/v0/tasks/{task.json()['task_id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["org_id"] == organization.org_id
    assert detail.json()["workspace_id"] == organization.workspace_id
    _verify_run_audit_and_range_validation(client, organization)


def _verify_run_audit_and_range_validation(
    client: TestClient, organization: OrganizationSetup
) -> None:
    audit = client.get(
        "/api/v0/audit-events",
        headers=organization.headers,
        params={
            "action": "agent.run.succeeded",
            "actor_principal_id": organization.principal_id,
        },
    )
    assert audit.status_code == 200
    assert audit.json()
    assert all(
        item["action"] == "agent.run.succeeded"
        and item["actor_principal_id"] == organization.principal_id
        for item in audit.json()
    )
    invalid_range = client.get(
        "/api/v0/audit-events",
        headers=organization.headers,
        params={
            "created_after": "2026-02-02T00:00:00Z",
            "created_before": "2026-02-01T00:00:00Z",
        },
    )
    assert invalid_range.status_code == 422


def _verify_approval_policy_management(
    client: TestClient, headers: dict[str, str], workspace_id: str
) -> str:
    policies = client.get("/api/v0/approval-policies", headers=headers)
    assert policies.status_code == 200
    policy = client.post(
        "/api/v0/approval-policies",
        headers=headers,
        json={
            "name": "High risk requires review",
            "required_risk_levels": ["high", "critical"],
            "allow_self_approval": False,
        },
    )
    assert policy.status_code == 201, policy.text
    policy_id = policy.json()["approval_policy_id"]
    assert policy.json()["workspace_id"] == workspace_id
    fetched = client.get(f"/api/v0/approval-policies/{policy_id}", headers=headers)
    assert fetched.status_code == 200
    updated = client.patch(
        f"/api/v0/approval-policies/{policy_id}",
        headers=headers,
        json={"allow_self_approval": True},
    )
    assert updated.status_code == 200
    assert updated.json()["allow_self_approval"] is True
    _verify_policy_admin_access(client, headers)
    runner_role = client.post(
        "/api/v0/roles",
        headers=headers,
        json={
            "name": "Workspace runner",
            "permissions": ["agents.run", "tasks.create"],
        },
    )
    assert runner_role.status_code == 201, runner_role.text
    return runner_role.json()["id"]


def _verify_policy_admin_access(
    client: TestClient, owner_headers: dict[str, str]
) -> None:
    admin = client.post(
        "/api/v0/users",
        headers=owner_headers,
        json={
            "email": "policy-admin@example.com",
            "display_name": "Policy Admin",
            "permissions": ["approval_policies.manage"],
        },
    )
    assert admin.status_code == 201, admin.text
    headers = {"Authorization": f"Bearer {admin.json()['access_token']}"}
    listing = client.get("/api/v0/approval-policies", headers=headers)
    assert listing.status_code == 200
    created = client.post(
        "/api/v0/approval-policies",
        headers=headers,
        json={"name": "Admin managed policy"},
    )
    assert created.status_code == 201


def _create_secondary_workspace(
    client: TestClient,
    owner_headers: dict[str, str],
    primary_workspace_id: str,
) -> WorkspaceSetup:
    response = client.post(
        "/api/v0/workspaces",
        headers=owner_headers,
        json={"name": "Research sandbox", "environment": "test"},
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["id"]
    headers = {**owner_headers, "X-Workspace-ID": workspace_id}
    visible = client.get("/api/v0/workspaces", headers=headers)
    assert {item["id"] for item in visible.json()} == {
        primary_workspace_id,
        workspace_id,
    }
    agent = client.post(
        "/api/v0/agents",
        headers=headers,
        json={
            "name": "Sandbox assistant",
            "instructions": "Answer using this workspace's knowledge.",
        },
    )
    assert agent.status_code == 201, agent.text
    task = client.post(
        "/api/v0/tasks",
        headers=headers,
        json={
            "title": "Workspace guide",
            "initial_message": "Summarize the workspace guide",
            "agent_id": agent.json()["id"],
        },
    )
    assert task.status_code == 202, task.text
    assert task.json()["workspace_id"] == workspace_id
    return WorkspaceSetup(workspace_id, headers, agent.json()["id"])


def _create_workspace_member(
    client: TestClient, workspace_headers: dict[str, str], runner_role_id: str
) -> MemberSetup:
    created = client.post(
        "/api/v0/users",
        headers=workspace_headers,
        json={
            "email": "member@example.com",
            "display_name": "Workspace Member",
            "role_id": runner_role_id,
        },
    )
    assert created.status_code == 201, created.text
    member = created.json()
    headers = {"Authorization": f"Bearer {member['access_token']}"}
    password = "member-password-123"
    password_set = client.put(
        "/api/v0/auth/password",
        headers=headers,
        json={"password": password},
    )
    assert password_set.status_code == 204
    login = client.post(
        "/api/v0/auth/login",
        json={"email": member["user"]["email"], "password": password},
    )
    assert login.status_code == 200, login.text
    session_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return MemberSetup(
        member["user"]["id"],
        member["principal"]["id"],
        headers,
        session_headers,
    )


def _verify_member_scope_restrictions(
    client: TestClient, member: MemberSetup, workspace: WorkspaceSetup
) -> None:
    workspaces = client.get("/api/v0/workspaces", headers=member.headers)
    assert [item["id"] for item in workspaces.json()] == [workspace.workspace_id]
    archived = client.get(
        "/api/v0/workspaces?include_archived=true", headers=member.headers
    )
    assert archived.status_code == 403
    assert client.get("/api/v0/agents", headers=member.headers).status_code == 403
    denied_policy = client.post(
        "/api/v0/approval-policies",
        headers=member.headers,
        json={"name": "Unauthorized policy"},
    )
    assert denied_policy.status_code == 403
    task = client.post(
        "/api/v0/tasks",
        headers=member.headers,
        json={
            "title": "Workspace guide",
            "initial_message": "Summarize the workspace guide",
            "agent_id": workspace.agent_id,
        },
    )
    assert task.status_code == 202, task.text
    assert task.json()["workspace_id"] == workspace.workspace_id
    assert client.get("/api/v0/artifacts", headers=member.headers).status_code == 404


def _verify_artifact_role_binding(
    client: TestClient,
    member: MemberSetup,
    workspace: WorkspaceSetup,
    other_workspace_id: str,
) -> None:
    role = client.post(
        "/api/v0/roles",
        headers=workspace.headers,
        json={"name": "Task reader", "permissions": ["tasks.view"]},
    )
    assert role.status_code == 201, role.text
    binding_payload = {
        "principal_id": member.principal_id,
        "role_id": role.json()["id"],
        "workspace_id": workspace.workspace_id,
    }
    binding = client.post(
        "/api/v0/role-bindings", headers=workspace.headers, json=binding_payload
    )
    assert binding.status_code == 201, binding.text
    repeated = client.post(
        "/api/v0/role-bindings", headers=workspace.headers, json=binding_payload
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == binding.json()["id"]
    assert client.get("/api/v0/tasks", headers=member.headers).status_code == 200
    removed = client.delete(
        f"/api/v0/role-bindings/{binding.json()['id']}", headers=workspace.headers
    )
    assert removed.status_code == 204
    assert client.get("/api/v0/tasks", headers=member.headers).status_code == 403
    cross_workspace = client.get(
        "/api/v0/agents",
        headers={**member.headers, "X-Workspace-ID": other_workspace_id},
    )
    assert cross_workspace.status_code == 403


def _verify_member_suspension_and_reactivation(
    client: TestClient, member: MemberSetup, workspace: WorkspaceSetup
) -> None:
    suspended = client.post(
        f"/api/v0/users/{member.user_id}/suspend", headers=workspace.headers
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["status"] == "suspended"
    assert client.get("/api/v0/workspaces", headers=member.headers).status_code == 401
    assert (
        client.get("/api/v0/workspaces", headers=member.session_headers).status_code
        == 401
    )
    reactivated = client.post(
        f"/api/v0/users/{member.user_id}/reactivate", headers=workspace.headers
    )
    assert reactivated.status_code == 200, reactivated.text
    reactivated_headers = {
        "Authorization": f"Bearer {reactivated.json()['access_token']}"
    }
    assert client.get("/api/v0/workspaces", headers=member.headers).status_code == 401
    assert (
        client.get("/api/v0/workspaces", headers=member.session_headers).status_code
        == 401
    )
    visible = client.get("/api/v0/workspaces", headers=reactivated_headers)
    assert [item["id"] for item in visible.json()] == [workspace.workspace_id]


def _verify_cross_tenant_isolation(
    client: TestClient, organization: OrganizationSetup
) -> None:
    other = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "Other Research",
            "owner_email": "other@example.com",
            "owner_display_name": "Other Owner",
        },
    )
    assert other.status_code == 201
    other_workspace_id = other.json()["workspace"]["id"]
    response = client.get(
        "/api/v0/agents",
        headers={
            **organization.headers,
            "X-Workspace-ID": other_workspace_id,
        },
    )
    assert response.status_code == 403
