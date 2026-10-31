import json
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    Workspace,
    WorkspaceStatus,
)
from tests.support.runtime import LocalPlatformRuntime


@dataclass(frozen=True)
class WorkspaceAdminScenario:
    """Resources needed to check workspace administrator boundaries."""

    workspace_id: str
    headers: dict[str, str]
    provider_id: str
    profile_id: str
    credential_id: str
    owner_user_id: str


def test_workspace_admin_cannot_manage_organization_wide_resources():
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        scenario = _prepare_workspace_admin_scenario(client)
        _assert_model_and_credential_denials(client, scenario)
        _assert_user_admin_denials(client, scenario)
        _assert_workspace_admin_listing_scope(client, scenario)


def _prepare_workspace_admin_scenario(client: TestClient) -> WorkspaceAdminScenario:
    onboarded = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "Scoped Administration Org",
            "owner_email": "owner@scoped-admin.example",
            "owner_display_name": "Scoped Admin Owner",
        },
    )
    assert onboarded.status_code == 201
    owner_headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}
    workspace = client.post(
        "/api/v0/workspaces",
        headers=owner_headers,
        json={"name": "Scoped admin workspace"},
    )
    assert workspace.status_code == 201
    workspace_id = workspace.json()["id"]
    workspace_headers = {**owner_headers, "X-Workspace-ID": workspace_id}
    provider = client.post(
        "/api/v0/model-providers",
        headers=owner_headers,
        json={
            "provider_type": "openai",
            "display_name": "Organization model provider",
            "api_key": "test-model-provider-key",
            "initial_model": "test-model-v1",
        },
    )
    assert provider.status_code == 201, provider.text
    profile_id = _profile_for_provider(client, owner_headers, provider.json()["id"])
    credential = client.post(
        "/api/v0/service-accounts",
        headers=owner_headers,
        json={"name": "Org service", "permissions": ["agents.run"]},
    )
    assert credential.status_code == 201
    admin = client.post(
        "/api/v0/users",
        headers=workspace_headers,
        json={
            "email": "workspace-admin@scoped-admin.example",
            "display_name": "Workspace Admin",
            "permissions": ["models.manage", "roles.manage", "users.manage"],
        },
    )
    assert admin.status_code == 201, admin.text
    return WorkspaceAdminScenario(
        workspace_id=workspace_id,
        headers={"Authorization": f"Bearer {admin.json()['access_token']}"},
        provider_id=provider.json()["id"],
        profile_id=profile_id,
        credential_id=credential.json()["credential"]["id"],
        owner_user_id=onboarded.json()["owner"]["id"],
    )


def _profile_for_provider(
    client: TestClient, owner_headers: dict[str, str], provider_id: str
) -> str:
    profiles = client.get("/api/v0/model-profiles", headers=owner_headers).json()
    return next(item["id"] for item in profiles if item["provider_id"] == provider_id)


def _assert_model_and_credential_denials(
    client: TestClient, scenario: WorkspaceAdminScenario
) -> None:
    headers = scenario.headers
    assert client.get("/api/v0/model-providers", headers=headers).status_code == 403
    assert client.get("/api/v0/model-profiles", headers=headers).status_code == 403
    assert (
        client.post(
            f"/api/v0/model-providers/{scenario.provider_id}/refresh-models",
            headers=headers,
        ).status_code
        == 403
    )
    assert client.post("/api/v0/models/refresh", headers=headers).status_code == 403
    assert client.get("/api/v0/api-credentials", headers=headers).status_code == 403
    assert (
        client.put(
            f"/api/v0/model-providers/{scenario.provider_id}/credential",
            headers=headers,
            json={"api_key": "unauthorized-replacement-key"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v0/model-profiles/{scenario.profile_id}",
            headers=headers,
            json={"status": "disabled"},
        ).status_code
        == 403
    )
    _assert_credential_mutation_denials(client, scenario)


def _assert_credential_mutation_denials(
    client: TestClient, scenario: WorkspaceAdminScenario
) -> None:
    assert (
        client.delete(
            f"/api/v0/api-credentials/{scenario.credential_id}",
            headers=scenario.headers,
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v0/api-credentials/{scenario.credential_id}/rotate",
            headers=scenario.headers,
        ).status_code
        == 403
    )


def _assert_user_admin_denials(
    client: TestClient, scenario: WorkspaceAdminScenario
) -> None:
    for action in ("suspend", "reactivate"):
        response = client.post(
            f"/api/v0/users/{scenario.owner_user_id}/{action}",
            headers=scenario.headers,
        )
        assert response.status_code == 403
    service_account = client.post(
        "/api/v0/service-accounts",
        headers=scenario.headers,
        json={"name": "Not permitted", "permissions": ["agents.run"]},
    )
    assert service_account.status_code == 403


def _assert_workspace_admin_listing_scope(
    client: TestClient, scenario: WorkspaceAdminScenario
) -> None:
    bindings = client.get("/api/v0/role-bindings", headers=scenario.headers)
    assert bindings.status_code == 200
    assert bindings.json()
    assert all(
        item["workspace_id"] == scenario.workspace_id for item in bindings.json()
    )
    users = client.get("/api/v0/users", headers=scenario.headers)
    assert users.status_code == 200
    assert [user["email"] for user in users.json()] == [
        "workspace-admin@scoped-admin.example"
    ]


def test_product_api_token_resolves_principal_and_enforces_assigned_permissions(
    monkeypatch,
):
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    now = datetime.now(UTC)
    runtime.principals.create(
        Principal(
            id="reader-principal",
            org_id="local-org",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            status=PrincipalStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    runtime.roles.create(
        Role(
            id="run-only-role",
            org_id="local-org",
            name="Run only",
            permissions=[Permission.AGENTS_RUN],
        )
    )
    runtime.role_bindings.create(
        RoleBinding(
            id="reader-role-binding",
            org_id="local-org",
            workspace_id="local-workspace",
            principal_id="reader-principal",
            role_id="run-only-role",
            created_at=now,
        )
    )
    runtime.workspaces.create(
        Workspace(
            id="restricted-hidden-workspace",
            org_id="local-org",
            name="Private workspace",
            status=WorkspaceStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    monkeypatch.delenv("WEAVES_API_TOKEN", raising=False)
    monkeypatch.setenv(
        "WEAVES_API_TOKENS",
        json.dumps([{"token": "read-only-secret", "principal_id": "reader-principal"}]),
    )
    client = TestClient(create_app(runtime))

    with client:
        assert client.get("/api/v0/runs").status_code == 401
        headers = {"Authorization": "Bearer read-only-secret"}
        assert client.get("/api/v0/runs", headers=headers).status_code == 200
        visible_workspaces = client.get("/api/v0/workspaces", headers=headers)
        assert [item["id"] for item in visible_workspaces.json()] == ["local-workspace"]
        assert client.get("/api/v0/agents", headers=headers).status_code == 403
        denied_workspace = client.post(
            "/api/v0/workspaces",
            headers=headers,
            json={"name": "Unauthorized workspace"},
        )
        assert denied_workspace.status_code == 403
        created = client.post(
            "/api/v0/runs",
            headers=headers,
            json={"task": "Summarize security basics"},
        )
        assert created.status_code == 201
        run_id = created.json()["run"]["run_id"]
        assert created.json()["run"]["requested_by_principal_id"] == "reader-principal"
        assert any(
            item.actor_principal_id == "reader-principal" and item.target_id == run_id
            for item in runtime.audit_events.list()
        )


def test_service_account_api_token_is_one_time_and_revocable(monkeypatch):
    monkeypatch.delenv("WEAVES_API_TOKEN", raising=False)
    monkeypatch.delenv("WEAVES_API_TOKENS", raising=False)
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        created = client.post(
            "/api/v0/service-accounts",
            json={
                "name": "Run service",
                "permissions": ["agents.run"],
                "expires_in_days": 7,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        token = payload["token"]
        credential_id = payload["credential"]["id"]
        assert "token_digest" not in payload["credential"]
        assert payload["credential"]["expires_at"] is not None

        service_headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v0/runs", headers=service_headers).status_code == 200
        assert client.get("/api/v0/agents", headers=service_headers).status_code == 403
        listed = client.get("/api/v0/api-credentials").json()
        assert len(listed) == 1
        assert "token_digest" not in listed[0]

        rotated = client.put(f"/api/v0/api-credentials/{credential_id}/rotate")
        assert rotated.status_code == 200
        rotation_payload = rotated.json()
        replacement = rotation_payload["credential"]
        replacement_token = rotation_payload["token"]
        assert replacement["principal_id"] == payload["principal"]["id"]
        assert replacement["expires_at"] == payload["credential"]["expires_at"]
        assert "token_digest" not in replacement
        assert client.get("/api/v0/runs", headers=service_headers).status_code == 401
        replacement_headers = {"Authorization": f"Bearer {replacement_token}"}
        assert (
            client.get("/api/v0/runs", headers=replacement_headers).status_code == 200
        )

        replacement_id = replacement["id"]
        revoked = client.delete(f"/api/v0/api-credentials/{replacement_id}")
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        assert (
            client.get("/api/v0/runs", headers=replacement_headers).status_code == 401
        )
        assert (
            client.delete(f"/api/v0/api-credentials/{replacement_id}").status_code
            == 200
        )
        assert (
            client.put(f"/api/v0/api-credentials/{replacement_id}/rotate").status_code
            == 409
        )


def test_execution_job_retry_requires_postgres_storage():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Retry requires a durable queue")
    client = TestClient(create_app(runtime))

    with client:
        response = client.post(
            f"/api/v0/execution-jobs/{job.job_id}/retry",
            headers={"Idempotency-Key": "retry-request-1"},
        )

    assert response.status_code == 503


def test_execution_job_cancel_api_exposes_requested_state():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Cancel through the API")
    claimed = runtime.execution_jobs.claim_next_queued(
        "api-cancel-worker", job.updated_at, job.org_id, job.workspace_id
    )
    assert claimed is not None
    client = TestClient(create_app(runtime))

    with client:
        response = client.post(f"/api/v0/execution-jobs/{job.job_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancel_requested"
    assert response.json()["worker_id"] == "api-cancel-worker"


def test_product_api_configures_agent_runs_and_inspects_platform_records():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        assert client.get("/api/v0/health").json() == {
            "status": "ok",
            "storage": "memory",
        }
        created = client.post(
            "/api/v0/agents",
            json={
                "name": "Policy helper",
                "description": "Answers policy questions.",
                "instructions": "Use approved handbook context and cite the sources.",
            },
        )
        assert created.status_code == 201
        agent = created.json()
        assert agent["current_version"]["instructions"].startswith("Use approved")
        first_version_id = agent["current_version"]["agent_version_id"]
        updated = client.patch(
            f"/api/v0/agents/{agent['id']}",
            json={"instructions": "Summarize approved context and name the sources."},
        )
        assert updated.status_code == 200
        assert updated.json()["current_version"]["version"] == 2
        assert runtime.agent_versions.get(first_version_id).instructions.startswith(
            "Use approved"
        )

        run_response = client.post(
            "/api/v0/runs",
            json={"agent_id": agent["id"], "task": "Summarize security basics"},
        )
        assert run_response.status_code == 201
        result = run_response.json()
        run_id = result["run"]["run_id"]
        assert result["run"]["status"] == "succeeded"
        assert (
            result["agent_run"]["agent_version_id"]
            == updated.json()["current_version"]["agent_version_id"]
        )
        assert result["artifacts"][0]["provenance"]

        listed = client.get("/api/v0/runs").json()
        assert listed[0]["run_id"] == run_id
        assert listed[0]["tool_invocations"][0]["capability_id"] == "knowledge.search"
        assert (
            client.get(f"/api/v0/runs/{run_id}/artifacts").json()[0]["run_id"] == run_id
        )
        assert client.get("/api/v0/artifacts").json()[0]["run_id"] == run_id
        events = client.get("/api/v0/audit-events").json()
        assert any(event["target_id"] == run_id for event in events)
