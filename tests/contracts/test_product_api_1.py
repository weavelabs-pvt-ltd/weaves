from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    ModelResponse,
    ModelUsage,
)
from tests.support.runtime import LocalPlatformRuntime


def test_native_oauth_connectors_reject_legacy_api_token_setup():
    client = TestClient(create_app(LocalPlatformRuntime()))
    retired_payloads = [
        {
            "plugin_id": "github",
            "display_name": "GitHub",
            "api_token": "legacy-token",
            "repositories": ["owner/repo"],
        },
        {
            "plugin_id": "slack",
            "display_name": "Slack",
            "user_token": "legacy-token",
            "channels": ["engineering"],
        },
        {
            "plugin_id": "jira",
            "display_name": "Jira",
            "api_token": "legacy-token",
            "site_url": "https://example.atlassian.net",
            "email": "user@example.test",
            "project_keys": ["WEAVES"],
        },
    ]

    with client:
        responses = [
            client.post("/api/v0/plugin-installations", json=payload)
            for payload in retired_payloads
        ]

    assert [response.status_code for response in responses] == [422, 422, 422]


def test_organization_onboarding_is_rate_limited_per_client_address():
    client = TestClient(create_app(LocalPlatformRuntime()))
    request = {
        "name": "Rate limited org",
        "owner_email": "owner@example.test",
        "owner_display_name": "Owner",
    }

    with client:
        for _ in range(3):
            response = client.post("/api/v0/onboarding/organizations", json=request)
            assert response.status_code == 201, response.text

        limited = client.post("/api/v0/onboarding/organizations", json=request)

    assert limited.status_code == 429
    assert 0 < int(limited.headers["retry-after"]) <= 3600
    assert limited.json()["detail"] == "organization onboarding is temporarily limited"


def test_organization_onboarding_can_be_disabled_for_deployed_workspaces(monkeypatch):
    monkeypatch.setenv("WEAVES_ALLOW_ORGANIZATION_ONBOARDING", "false")
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        response = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Closed signup org",
                "owner_email": "owner@example.test",
                "owner_display_name": "Owner",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Organization onboarding is disabled"


def test_organization_onboarding_token_gates_public_provisioning(monkeypatch):
    monkeypatch.setenv("WEAVES_ORGANIZATION_ONBOARDING_TOKEN", "expected-secret")
    client = TestClient(create_app(LocalPlatformRuntime()))
    payload = {
        "name": "Token gated org",
        "owner_email": "owner@example.test",
        "owner_display_name": "Owner",
    }

    with client:
        denied = client.post("/api/v0/onboarding/organizations", json=payload)
        created = client.post(
            "/api/v0/onboarding/organizations",
            json=payload,
            headers={"X-Weaves-Onboarding-Token": "expected-secret"},
        )

    assert denied.status_code == 404
    assert created.status_code == 201, created.text


def test_engineering_agent_template_creates_a_scoped_runnable_agent(monkeypatch):
    runtime = LocalPlatformRuntime()
    captured_requests = []

    def complete(request, selected_binding):
        captured_requests.append(request)
        return ModelResponse(
            provider_id=selected_binding.provider.id,
            model_id=selected_binding.profile.model,
            content="Research complete.",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )

    monkeypatch.setattr(runtime.model, "complete", complete)
    client = TestClient(create_app(runtime))
    with client:
        onboarded = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Template test org",
                "owner_email": "template-owner@example.test",
                "owner_display_name": "Template Owner",
            },
        )
        assert onboarded.status_code == 201, onboarded.text
        headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}

        _assert_engineering_template_options(client, headers)

        created = client.post(
            "/api/v0/agent-templates/engineering-assistant/agents",
            headers=headers,
            json={
                "allowed_capability_ids": [],
                "max_runtime_seconds": 120,
                "max_tool_calls": 2,
                "max_output_tokens": 600,
            },
        )
        assert created.status_code == 201, created.text
        agent = _assert_created_engineering_agent(created)

        updated = client.patch(
            f"/api/v0/agents/{agent['id']}",
            headers=headers,
            json={
                "budget": {
                    "max_runtime_seconds": 180,
                    "max_tool_calls": 1,
                    "max_output_tokens": 300,
                }
            },
        )
        assert updated.status_code == 200, updated.text
        agent = updated.json()
        assert agent["current_version"]["version"] == 2
        assert agent["current_version"]["budget"]["max_runtime_seconds"] == 180
        assert agent["current_version"]["budget"]["max_tool_calls"] == 1

        task_response = client.post(
            "/api/v0/tasks",
            headers=headers,
            json={
                "agent_id": agent["id"],
                "title": "Working agreements",
                "initial_message": "Summarize engineering working agreements",
            },
        )
        assert task_response.status_code == 202, task_response.text
        runtime.task_worker_service.process_next_turn("product-api-test")
        detail = client.get(
            f"/api/v0/tasks/{task_response.json()['task_id']}", headers=headers
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["latest_turn_status"] == "succeeded"
        turn = runtime.task_turns.get(detail.json()["latest_turn_id"])
        assert turn.agent_run_id is not None
        assert runtime.agent_runs.get(turn.agent_run_id).status.value == "succeeded"
        assert "Research skill:" in captured_requests[0].messages[0].content


def _assert_engineering_template_options(client, headers):
    """Check template recommendations and reject a skill outside the catalog."""
    templates = client.get("/api/v0/agent-templates", headers=headers)
    assert templates.status_code == 200, templates.text
    template = next(
        item
        for item in templates.json()
        if item["template_id"] == "engineering-assistant"
    )
    assert "github.issues.search" in template["recommended_capability_ids"]
    assert "slack.messages.search" in template["recommended_capability_ids"]
    assert "jira.issues.search" in template["recommended_capability_ids"]
    assert "evidence-based-research" in template["recommended_skill_ids"]
    invalid_skill = client.post(
        "/api/v0/agents",
        headers=headers,
        json={
            "name": "Invalid skill agent",
            "instructions": "This should not be stored.",
            "skill_ids": ["not-installed-or-defined"],
        },
    )
    assert invalid_skill.status_code == 422


def _assert_created_engineering_agent(created):
    """Verify template defaults and requested runtime budgets on creation."""
    agent = created.json()
    assert agent["name"] == "Engineering Assistant"
    assert agent["current_version"]["persona"] == (
        "A careful engineering operations assistant."
    )
    assert agent["current_version"]["skills"][0]["skill_id"] == (
        "evidence-based-research"
    )
    assert agent["current_version"]["allowed_capability_ids"] == []
    budget = agent["current_version"]["budget"]
    assert budget["max_runtime_seconds"] == 120
    assert budget["max_tool_calls"] == 2
    assert budget["max_output_tokens"] == 600
    return agent


def test_agent_output_schema_can_be_configured_and_returned_through_api(monkeypatch):
    runtime = LocalPlatformRuntime()

    def complete(request, selected_binding):
        assert '"required": ["answer"]' in request.messages[0].content
        return ModelResponse(
            provider_id=selected_binding.provider.id,
            model_id=selected_binding.profile.model,
            content='{"answer":"approved"}',
            usage=ModelUsage(input_tokens=10, output_tokens=4, total_tokens=14),
        )

    monkeypatch.setattr(runtime.model, "complete", complete)
    client = TestClient(create_app(runtime))
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    with client:
        onboarded = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Structured output org",
                "owner_email": "structured-owner@example.test",
                "owner_display_name": "Structured Owner",
            },
        )
        assert onboarded.status_code == 201, onboarded.text
        headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}
        created = client.post(
            "/api/v0/agents",
            headers=headers,
            json={
                "name": "Structured responder",
                "instructions": "Answer using the configured response shape.",
                "allowed_capability_ids": [],
                "output_schema": schema,
            },
        )
        assert created.status_code == 201, created.text
        agent = created.json()
        assert agent["current_version"]["output_schema"] == schema

        task_response = client.post(
            "/api/v0/tasks",
            headers=headers,
            json={
                "agent_id": agent["id"],
                "title": "Structured response",
                "initial_message": "Answer approved",
            },
        )
        assert task_response.status_code == 202, task_response.text
        runtime.task_worker_service.process_next_turn("structured-output-test")
        task = client.get(
            f"/api/v0/tasks/{task_response.json()['task_id']}", headers=headers
        ).json()
        turn = runtime.task_turns.get(task["latest_turn_id"])
        agent_run = runtime.agent_runs.get(turn.agent_run_id)
        assert agent_run.output_summary["structured_output"] == {"answer": "approved"}

        cleared = client.patch(
            f"/api/v0/agents/{agent['id']}",
            headers=headers,
            json={"output_schema": None},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["current_version"]["version"] == 2
        assert cleared.json()["current_version"]["output_schema"] is None

        _assert_invalid_output_schema_rejected(client, headers)


def _assert_invalid_output_schema_rejected(client, headers):
    """Reject output schemas that do not follow the supported JSON schema subset."""
    invalid = client.post(
        "/api/v0/agents",
        headers=headers,
        json={
            "name": "Invalid schema agent",
            "instructions": "Return JSON.",
            "allowed_capability_ids": [],
            "output_schema": {"type": "unknown"},
        },
    )
    assert invalid.status_code == 422


def test_product_api_bearer_token_gate_preserves_health_check(monkeypatch):
    monkeypatch.setenv("WEAVES_API_TOKEN", "test-platform-token")
    monkeypatch.delenv("WEAVES_API_TOKENS", raising=False)
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        assert client.get("/api/v0/health").status_code == 200
        assert client.get("/api/v0/organizations").status_code == 401
        invalid = client.get(
            "/api/v0/organizations",
            headers={"Authorization": "Bearer incorrect-token"},
        )
        assert invalid.status_code == 401
        assert invalid.headers["www-authenticate"] == "Bearer"
        authorized = client.get(
            "/api/v0/organizations",
            headers={"Authorization": "Bearer test-platform-token"},
        )
        assert authorized.status_code == 200


def test_postgres_mode_fails_closed_without_bearer_but_allows_bootstrap(monkeypatch):
    monkeypatch.delenv("WEAVES_API_TOKEN", raising=False)
    monkeypatch.delenv("WEAVES_API_TOKENS", raising=False)
    runtime = LocalPlatformRuntime()
    runtime.storage_mode = "postgres"
    client = TestClient(create_app(runtime))

    with client:
        assert client.get("/api/v0/health").status_code == 200
        protected = client.get("/api/v0/organizations")
        assert protected.status_code == 401
        assert protected.headers["www-authenticate"] == "Bearer"

        onboarded = client.post(
            "/api/v0/onboarding/organizations",
            json={
                "name": "Fail Closed Org",
                "owner_email": "owner@fail-closed.test",
                "owner_display_name": "Owner",
            },
        )
        assert onboarded.status_code == 201, onboarded.text
        owner_headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}
        assert (
            client.get("/api/v0/organizations", headers=owner_headers).status_code
            == 200
        )
