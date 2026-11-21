from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.organizations import onboard
from tests.support.runtime import LocalPlatformRuntime


def test_workspace_lifecycle_requires_admin_and_preserves_active_workspace():
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        onboarded = onboard(
            client,
            name="Lifecycle Org",
            owner_email="owner@lifecycle.test",
            owner_display_name="Owner",
        )
        owner_headers = {"Authorization": f"Bearer {onboarded['access_token']}"}
        default_workspace_id = onboarded["workspace"]["id"]

        last_workspace_archive = client.patch(
            f"/api/v0/workspaces/{default_workspace_id}",
            headers=owner_headers,
            json={"status": "archived"},
        )
        assert last_workspace_archive.status_code == 422

        created = client.post(
            "/api/v0/workspaces",
            headers=owner_headers,
            json={"name": "Temporary", "environment": "test"},
        )
        assert created.status_code == 201, created.text
        temporary_id = created.json()["id"]

        archived = client.patch(
            f"/api/v0/workspaces/{temporary_id}",
            headers=owner_headers,
            json={"status": "archived"},
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"
        assert {
            item["id"]
            for item in client.get("/api/v0/workspaces", headers=owner_headers).json()
        } == {default_workspace_id}
        all_workspaces = client.get(
            "/api/v0/workspaces?include_archived=true", headers=owner_headers
        )
        assert all_workspaces.status_code == 200
        assert {item["id"] for item in all_workspaces.json()} == {
            default_workspace_id,
            temporary_id,
        }

        restored = client.patch(
            f"/api/v0/workspaces/{temporary_id}",
            headers=owner_headers,
            json={"status": "active", "name": "Restored"},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "active"
        assert restored.json()["name"] == "Restored"
        assert {
            item["id"]
            for item in client.get("/api/v0/workspaces", headers=owner_headers).json()
        } == {default_workspace_id, temporary_id}

        audit = client.get(
            "/api/v0/audit-events",
            headers={
                **owner_headers,
                "X-Workspace-ID": temporary_id,
            },
            params={"action": "workspace.updated", "target_id": temporary_id},
        )
        assert audit.status_code == 200
        assert len(audit.json()) == 2


def test_member_provisioning_does_not_expose_a_signin_credential():
    with TestClient(create_app(LocalPlatformRuntime())) as client:
        roles = client.get("/api/v0/roles").json()
        role = next(r for r in roles if r["system_role_key"] == "user")
        created = client.post(
            "/api/v0/users",
            json={
                "email": "new.member@example.test",
                "display_name": "New member",
                "role_id": role["id"],
            },
        )
        assert created.status_code == 201, created.text
        assert "access_token" not in created.json()
        assert created.json()["user"]["status"] == "active"
