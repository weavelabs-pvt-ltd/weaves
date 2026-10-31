import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    ModelProviderStatus,
    ModelProviderType,
)
from tests.support.runtime import LocalPlatformRuntime


def test_product_api_approves_jira_comment_before_sending_it():
    posted_comments = []
    provider_state = {"deny_healthcheck": False}

    def respond(request):
        if request.url.path.endswith("/project/WEAVES"):
            if provider_state["deny_healthcheck"]:
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"key": "WEAVES"})
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/comment"):
            posted_comments.append(request)
            return httpx.Response(201, json={"id": "comment-77"})
        raise AssertionError(f"unexpected Jira request: {request.url}")

    runtime = LocalPlatformRuntime()
    jira_client = httpx.Client(transport=httpx.MockTransport(respond))
    runtime.jira_plugin._client = jira_client
    client = TestClient(create_app(runtime))
    try:
        with client:
            installation_id = _install_healthy_jira(client, posted_comments)
            _assert_failed_credential_rotation(
                client, runtime, installation_id, provider_state
            )
            _assert_recovered_credential_rotation(
                client, installation_id, provider_state
            )
            approval_id = _propose_jira_comment(client, installation_id)
            _assert_approval_requires_independent_reviewer(
                client, approval_id, posted_comments
            )
            _assert_reviewer_executes_approved_comment(
                client, approval_id, posted_comments
            )
    finally:
        jira_client.close()


def _install_healthy_jira(client, posted_comments):
    """Install Jira and verify its read-only health check sends no comments."""
    response = client.post(
        "/api/v0/plugin-installations",
        json={
            "plugin_id": "jira",
            "display_name": "Engineering Jira",
            "api_token": "jira-test-token",
            "site_url": "https://example.atlassian.net",
            "email": "dev@example.com",
            "project_keys": ["WEAVES"],
        },
    )
    assert response.status_code == 201
    installation_id = response.json()["plugin_installation_id"]
    assert "auth_ref" not in response.json()
    healthcheck = client.post(
        f"/api/v0/plugin-installations/{installation_id}/healthcheck"
    )
    assert healthcheck.status_code == 200
    assert healthcheck.json()["project"] == "WEAVES"
    assert not posted_comments
    return installation_id


def _assert_failed_credential_rotation(client, runtime, installation_id, state):
    """Check failed health turns over credentials and hides secret references."""
    old_secret_ref = runtime.installations.get(installation_id).auth_ref
    state["deny_healthcheck"] = True
    response = client.put(
        f"/api/v0/plugin-installations/{installation_id}/credential",
        json={"api_key": "invalid-test-token"},
    )
    assert response.status_code == 200
    assert "auth_ref" not in response.json()
    failure = client.post(f"/api/v0/plugin-installations/{installation_id}/healthcheck")
    assert failure.status_code == 502
    installations = client.get("/api/v0/plugin-installations").json()
    failed = next(
        item
        for item in installations
        if item["plugin_installation_id"] == installation_id
    )
    assert failed["status"] == "error"
    assert failed["last_healthcheck_at"] is not None
    assert old_secret_ref is not None
    with pytest.raises(KeyError):
        runtime.secret_store.get(old_secret_ref)


def _assert_recovered_credential_rotation(client, installation_id, state):
    """Restore the credential and verify health reactivates the installation."""
    state["deny_healthcheck"] = False
    response = client.put(
        f"/api/v0/plugin-installations/{installation_id}/credential",
        json={"api_key": "jira-test-token"},
    )
    assert response.status_code == 200
    healthcheck = client.post(
        f"/api/v0/plugin-installations/{installation_id}/healthcheck"
    )
    assert healthcheck.status_code == 200
    assert healthcheck.json()["status"] == "ok"
    installations = client.get("/api/v0/plugin-installations").json()
    recovered = next(
        item
        for item in installations
        if item["plugin_installation_id"] == installation_id
    )
    assert recovered["status"] == "active"


def _propose_jira_comment(client, installation_id):
    """Configure the Jira capability, create a run, and request approval."""
    policies = client.get("/api/v0/approval-policies")
    assert policies.status_code == 200
    policy_id = policies.json()[0]["approval_policy_id"]
    strict_policy = client.patch(
        f"/api/v0/approval-policies/{policy_id}",
        json={"allow_self_approval": False},
    )
    assert strict_policy.status_code == 200
    enabled = client.patch(
        f"/api/v0/plugin-installations/{installation_id}",
        json={"enabled_capability_ids": ["jira.issues.search", "jira.issues.comment"]},
    )
    assert enabled.status_code == 200
    agent = client.post(
        "/api/v0/agents",
        json={
            "name": "Jira assistant",
            "instructions": "Use context and suggest next steps.",
            "allowed_capability_ids": ["knowledge.search", "jira.issues.comment"],
        },
    )
    assert agent.status_code == 201
    run = client.post(
        "/api/v0/runs",
        json={"agent_id": agent.json()["id"], "task": "Review release readiness"},
    )
    assert run.status_code == 201
    run_id = run.json()["run"]["run_id"]
    proposal = client.post(
        f"/api/v0/runs/{run_id}/actions",
        json={
            "capability_id": "jira.issues.comment",
            "issue_key": "WEAVES-17",
            "comment": "The release checklist is ready.",
        },
    )
    assert proposal.status_code == 201
    assert proposal.json()["tool_invocation"]["status"] == "awaiting_approval"
    return proposal.json()["approval"]["approval_request_id"]


def _assert_approval_requires_independent_reviewer(client, approval_id, comments):
    """Verify pending approval blocks action and rejects self-approval."""
    pending = client.get("/api/v0/approvals?status=pending")
    assert pending.status_code == 200
    assert pending.json()[0]["approval_request_id"] == approval_id
    decision = client.post(
        f"/api/v0/approvals/{approval_id}/decision",
        json={"approved": True, "decision_comment": "Self approval denied."},
    )
    assert decision.status_code == 403
    assert not comments


def _assert_reviewer_executes_approved_comment(client, approval_id, comments):
    """Approve the Jira action as a different principal and verify one write."""
    reviewer = client.post(
        "/api/v0/users",
        json={
            "email": "reviewer@example.com",
            "display_name": "Independent Reviewer",
            "permissions": ["actions.approve"],
        },
    )
    assert reviewer.status_code == 201, reviewer.text
    headers = {"Authorization": f"Bearer {reviewer.json()['access_token']}"}
    decision = client.post(
        f"/api/v0/approvals/{approval_id}/decision",
        headers=headers,
        json={"approved": True, "decision_comment": "Looks good."},
    )
    assert decision.status_code == 200
    assert decision.json()["approval"]["status"] == "approved"
    assert decision.json()["tool_invocation"]["status"] == "succeeded"
    assert len(comments) == 1


def test_legacy_model_refresh_selects_a_configured_provider(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.model_service.create_model_provider(
        provider_type=ModelProviderType.ANTHROPIC,
        display_name="Anthropic test provider",
        api_key="anthropic-test-key",
        initial_model="claude-initial",
    )
    anthropic_adapter = runtime.model_adapters[ModelProviderType.ANTHROPIC]
    monkeypatch.setattr(
        anthropic_adapter,
        "list_models",
        lambda _provider: [{"id": "claude-discovered", "owned_by": "anthropic"}],
    )
    client = TestClient(create_app(runtime))

    with client:
        refreshed = client.post("/api/v0/models/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert [profile["model"] for profile in refreshed.json()] == [
            "claude-discovered"
        ]

        gemini = runtime.model_service.create_model_provider(
            provider_type=ModelProviderType.GEMINI,
            display_name="Gemini test provider",
            api_key="gemini-test-key",
            initial_model="gemini-initial",
        )
        gemini_adapter = runtime.model_adapters[ModelProviderType.GEMINI]
        monkeypatch.setattr(
            gemini_adapter,
            "list_models",
            lambda _provider: [{"id": "gemini-discovered", "owned_by": "google"}],
        )

        ambiguous = client.post("/api/v0/models/refresh")
        assert ambiguous.status_code == 422
        assert "provider_id is required" in ambiguous.json()["detail"]

        selected = client.post(
            "/api/v0/models/refresh", params={"provider_id": gemini.id}
        )
        assert selected.status_code == 200, selected.text
        assert [profile["model"] for profile in selected.json()] == [
            "gemini-discovered"
        ]

        disabled_provider = runtime.providers.get(gemini.id)
        runtime.providers.put(
            disabled_provider.model_copy(
                update={"status": ModelProviderStatus.DISABLED}
            )
        )
        disabled_refresh = client.post(
            "/api/v0/models/refresh", params={"provider_id": gemini.id}
        )
        assert disabled_refresh.status_code == 409
        assert disabled_refresh.json()["detail"] == "Model provider is disabled"
