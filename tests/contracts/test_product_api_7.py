from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.member_login import member_token
from tests.support.runtime import LocalPlatformRuntime
from tests.support.task_execution import run_task


def test_product_api_bounds_requests_and_rejects_unknown_agents():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        too_short = client.post(
            "/api/v0/agents",
            json={"name": "A", "instructions": "Do work."},
        )
        assert too_short.status_code == 422
        unknown = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "missing-agent",
                "title": "Unknown agent",
                "initial_message": "Run this task",
            },
        )
        assert unknown.status_code == 404
        assert client.post("/api/v0/execution-jobs").status_code == 404


def test_product_api_manages_memory_and_agent_scope_policy():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        created_memory = client.post(
            "/api/v0/memory-items",
            json={
                "title": "Release owner",
                "text": "The release owner reviews the release checklist.",
                "scope": "organization",
            },
        )
        assert created_memory.status_code == 201
        memory_id = created_memory.json()["memory_id"]
        assert client.get("/api/v0/memory-items").json()[0]["scope"] == "organization"

        agent_response = client.post(
            "/api/v0/agents",
            json={
                "name": "Org memory helper",
                "instructions": "Answer from approved organization memory.",
                "memory_scopes": ["organization"],
            },
        )
        assert agent_response.status_code == 201
        policy = agent_response.json()["current_version"]["memory_policy"]
        assert policy["allowed_scopes"] == ["organization"]

        _task, _turn, agent_run, artifacts = run_task(
            client,
            runtime,
            "Who reviews the release checklist?",
            agent_id=agent_response.json()["id"],
        )
        assert f"memory:{memory_id}" in agent_run.output_summary["source_ids"]
        assert "Retrieved sources:" in agent_run.output_summary["response"]
        assert any(
            item.source_type == "memory.item" for item in artifacts[0].provenance
        )

        deleted = client.delete(f"/api/v0/memory-items/{memory_id}")
        assert deleted.status_code == 204
        assert client.get("/api/v0/memory-items").json() == []


def test_memory_management_is_limited_to_organization_or_selected_workspace():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        context = _create_memory_scope_context(client)
        memories = _create_scoped_memory_items(client, context)
        _assert_memory_scope_boundaries(client, context, memories)
        _assert_memory_retention_validation(client, context)


def _create_memory_scope_context(client):
    """Create two workspace editors and return their scoped credentials."""
    onboarded = client.post(
        "/api/v0/onboarding/organizations",
        json={
            "name": "Memory Scope Org",
            "owner_email": "owner@memory-scope.example",
            "owner_display_name": "Memory Scope Owner",
        },
    )
    assert onboarded.status_code == 201
    owner_headers = {"Authorization": f"Bearer {onboarded.json()['access_token']}"}
    first_workspace_id = onboarded.json()["workspace"]["id"]
    second = client.post(
        "/api/v0/workspaces",
        headers=owner_headers,
        json={"name": "Second memory workspace"},
    )
    assert second.status_code == 201
    second_workspace_id = second.json()["id"]
    first_headers = _create_workspace_memory_editor(
        client, owner_headers, first_workspace_id, "first", "First Workspace Editor"
    )
    second_headers = _create_workspace_memory_editor(
        client,
        owner_headers,
        second_workspace_id,
        "second",
        "Second Workspace Editor",
    )
    return owner_headers, first_workspace_id, first_headers, second_headers


def _create_workspace_memory_editor(client, owner_headers, workspace_id, name, title):
    """Create one workspace-scoped editor with memory management permissions."""
    response = client.post(
        "/api/v0/users",
        headers={**owner_headers, "X-Workspace-ID": workspace_id},
        json={
            "email": f"{name}@memory-scope.example",
            "display_name": title,
            "permissions": ["plugins.manage"],
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {member_token(client, response.json())}"}


def _create_scoped_memory_items(client, context):
    """Create workspace and organization memories for visibility assertions."""
    owner_headers, _, first_headers, second_headers = context
    first = client.post(
        "/api/v0/memory-items",
        headers=first_headers,
        json={
            "title": "First space note",
            "text": "First workspace detail.",
            "retention_class": "regulated",
        },
    )
    second = client.post(
        "/api/v0/memory-items",
        headers=second_headers,
        json={"title": "Second space note", "text": "Second workspace detail."},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["retention_class"] == "regulated"
    shared = client.post(
        "/api/v0/memory-items",
        headers=owner_headers,
        json={
            "title": "Shared organization note",
            "text": "Organization-wide detail.",
            "scope": "organization",
        },
    )
    assert shared.status_code == 201
    return first.json(), second.json(), shared.json()


def _assert_memory_scope_boundaries(client, context, memories):
    """Verify visibility and deletion respect workspace and organization scope."""
    _, workspace_id, first_headers, _ = context
    first, second, shared = memories
    visible = client.get(
        "/api/v0/memory-items",
        headers={**first_headers, "X-Workspace-ID": workspace_id},
    )
    visible_ids = {item["memory_id"] for item in visible.json()}
    assert visible_ids == {first["memory_id"], shared["memory_id"]}
    cross_workspace_delete = client.delete(
        f"/api/v0/memory-items/{second['memory_id']}", headers=first_headers
    )
    assert cross_workspace_delete.status_code == 404
    denied = client.post(
        "/api/v0/memory-items",
        headers=first_headers,
        json={
            "title": "Unscoped note",
            "text": "Workspace role must not publish this.",
            "scope": "organization",
        },
    )
    assert denied.status_code == 403


def _assert_memory_retention_validation(client, context):
    """Reject ephemeral memory without an expiry or with a naive timestamp."""
    owner_headers, workspace_id, _, _ = context
    headers = {**owner_headers, "X-Workspace-ID": workspace_id}
    missing_expiry = client.post(
        "/api/v0/memory-items",
        headers=headers,
        json={
            "title": "Unbounded ephemeral note",
            "text": "Expiry must be explicit.",
            "retention_class": "ephemeral",
        },
    )
    assert missing_expiry.status_code == 422
    naive_expiry = client.post(
        "/api/v0/memory-items",
        headers=headers,
        json={
            "title": "Timezone-less expiry",
            "text": "Expiry must include a timezone.",
            "retention_class": "ephemeral",
            "expires_at": "2030-01-01T00:00:00",
        },
    )
    assert naive_expiry.status_code == 422


def test_model_profile_rates_produce_per_run_cost_estimates():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        agent = client.post(
            "/api/v0/agents",
            json={
                "name": "Costed assistant",
                "instructions": "Answer with the available handbook context.",
            },
        ).json()
        profile_id = agent["current_version"]["model_profile_id"]
        configured = client.patch(
            f"/api/v0/model-profiles/{profile_id}",
            json={
                "input_cost_per_million_tokens_usd": "2.5",
                "output_cost_per_million_tokens_usd": "10",
            },
        )
        assert configured.status_code == 200

        _task, _turn, agent_run, _artifacts = run_task(
            client, runtime, "Summarize security basics", agent_id=agent["id"]
        )
        expected_cost = (
            Decimal(agent_run.input_tokens) * Decimal("2.5")
            + Decimal(agent_run.output_tokens) * Decimal("10")
        ) / Decimal(1_000_000)
        assert agent_run.estimated_cost_usd == expected_cost


def test_model_profile_cost_budget_records_usage_and_fails_over_budget_run():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        agent = client.post(
            "/api/v0/agents",
            json={
                "name": "Budgeted assistant",
                "instructions": "Be concise.",
                "max_run_cost_usd": "0.000001",
            },
        ).json()
        profile_id = agent["current_version"]["model_profile_id"]
        configured = client.patch(
            f"/api/v0/model-profiles/{profile_id}",
            json={
                "cost_budget_usd": "0.000001",
                "input_cost_per_million_tokens_usd": "1",
                "output_cost_per_million_tokens_usd": "1",
            },
        )
        assert configured.status_code == 200

        _task, turn, agent_run, _artifacts = run_task(
            client, runtime, "Explain the knowledge base", agent_id=agent["id"]
        )
        assert turn.status.value == "failed"
        assert agent_run.status.value == "failed"
        assert agent_run.input_tokens > 0
        assert agent_run.output_tokens > 0
        assert agent_run.estimated_cost_usd > Decimal("0.000001")


def test_cost_budget_requires_configured_input_and_output_rates():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        agent = client.post(
            "/api/v0/agents",
            json={"name": "Unpriced assistant", "instructions": "Be concise."},
        ).json()
        profile_id = agent["current_version"]["model_profile_id"]
        configured = client.patch(
            f"/api/v0/model-profiles/{profile_id}",
            json={"cost_budget_usd": "1"},
        )
        assert configured.status_code == 200

        _task, turn, _agent_run, _artifacts = run_task(
            client, runtime, "Explain the knowledge base", agent_id=agent["id"]
        )
        assert turn.status.value == "failed"
        assert turn.error is not None
        assert turn.error.code == "task.agent_run_failed"


def test_task_thread_memory_isolated_between_task_conversations():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        agent = client.post(
            "/api/v0/agents",
            json={
                "name": "Scoped memory helper",
                "instructions": "Use matching conversation memory.",
                "memory_scopes": ["thread"],
            },
        )
        assert agent.status_code == 201
        first_task = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": agent.json()["id"],
                "title": "Release conversation A",
                "initial_message": "What window did this conversation approve?",
            },
        )
        second_task = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": agent.json()["id"],
                "title": "Release conversation B",
                "initial_message": "What window did this conversation approve?",
            },
        )
        assert first_task.status_code == second_task.status_code == 202
        first_thread = first_task.json()["thread_id"]
        memory = client.post(
            "/api/v0/memory-items",
            json={
                "title": "Conversation decision",
                "text": "This conversation approved the blue release window.",
                "scope": "thread",
                "scope_ref": first_thread,
            },
        )
        assert memory.status_code == 201
        first_turn = runtime.task_worker_service.process_next_turn("memory-worker")
        second_turn = runtime.task_worker_service.process_next_turn("memory-worker")
        assert first_turn is not None and second_turn is not None
        first_run = runtime.agent_runs.get(first_turn.agent_run_id)
        second_run = runtime.agent_runs.get(second_turn.agent_run_id)
        source_id = f"memory:{memory.json()['memory_id']}"
        assert source_id in first_run.output_summary["source_ids"]
        assert source_id not in second_run.output_summary["source_ids"]
