from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


def test_product_api_bounds_requests_and_rejects_unknown_agents():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        too_short = client.post(
            "/api/v0/agents",
            json={"name": "A", "instructions": "Do work."},
        )
        assert too_short.status_code == 422
        unknown = client.post(
            "/api/v0/runs",
            json={"agent_id": "missing-agent", "task": "Run this task"},
        )
        assert unknown.status_code == 404
        queued = client.post(
            "/api/v0/execution-jobs", json={"task": "Run a durable job"}
        )
        assert queued.status_code == 503
        queued_workflow = client.post(
            "/api/v0/execution-jobs",
            json={
                "task": "Run a durable workflow",
                "workflow_id": "local-assistant-workflow",
            },
        )
        assert queued_workflow.status_code == 503
        ambiguous = client.post(
            "/api/v0/execution-jobs",
            json={
                "task": "Run a durable job",
                "agent_id": "local-assistant",
                "workflow_id": "local-assistant-workflow",
            },
        )
        assert ambiguous.status_code == 422


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

        run = client.post(
            "/api/v0/runs",
            json={
                "agent_id": agent_response.json()["id"],
                "task": "Who reviews the release checklist?",
            },
        )
        assert run.status_code == 201
        agent_run = run.json()["agent_run"]
        assert f"memory:{memory_id}" in agent_run["output_summary"]["source_ids"]
        assert "Retrieved sources:" in agent_run["output_summary"]["response"]
        assert any(
            item["source_type"] == "memory.item"
            for item in run.json()["artifacts"][0]["provenance"]
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
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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
    client = TestClient(create_app(LocalPlatformRuntime()))

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

        result = client.post(
            "/api/v0/runs",
            json={"agent_id": agent["id"], "task": "Summarize security basics"},
        )
        assert result.status_code == 201
        agent_run = result.json()["agent_run"]
        expected_cost = (
            Decimal(agent_run["input_tokens"]) * Decimal("2.5")
            + Decimal(agent_run["output_tokens"]) * Decimal("10")
        ) / Decimal(1_000_000)
        assert Decimal(agent_run["estimated_cost_usd"]) == expected_cost


def test_model_profile_cost_budget_records_usage_and_fails_over_budget_run():
    client = TestClient(create_app(LocalPlatformRuntime()))

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

        result = client.post(
            "/api/v0/runs",
            json={"agent_id": agent["id"], "task": "Explain the knowledge base"},
        )
        assert result.status_code == 422
        assert "provider request already completed" in result.json()["detail"]

        run = client.get("/api/v0/runs").json()[0]
        assert run["status"] == "failed"
        agent_run = client.get(f"/api/v0/runs/{run['run_id']}").json()["agent_runs"][0]
        assert agent_run["status"] == "failed"
        assert agent_run["input_tokens"] > 0
        assert agent_run["output_tokens"] > 0
        assert Decimal(agent_run["estimated_cost_usd"]) > Decimal("0.000001")


def test_cost_budget_requires_configured_input_and_output_rates():
    client = TestClient(create_app(LocalPlatformRuntime()))

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

        result = client.post(
            "/api/v0/runs",
            json={"agent_id": agent["id"], "task": "Explain the knowledge base"},
        )
        assert result.status_code == 422
        assert "require input and output token rates" in result.json()["detail"]
        assert client.get("/api/v0/runs").json() == []


def test_thread_and_workflow_memory_do_not_cross_execution_scopes():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        scope = _create_thread_and_workflow_memories(client)
        runs = _run_in_separate_memory_scopes(client, scope)
        _assert_thread_run_history(client, scope, runs)
        _assert_closed_thread_rejects_runs(client, scope)


def _create_thread_and_workflow_memories(client):
    """Create workflow- and thread-scoped memories for one agent."""
    agent = client.post(
        "/api/v0/agents",
        json={
            "name": "Scoped memory helper",
            "instructions": "Use the matching conversation and workflow memory.",
            "memory_scopes": ["thread", "workflow"],
        },
    )
    assert agent.status_code == 201
    agent_id = agent.json()["id"]
    workflow_id = agent.json()["current_version"]["allowed_workflow_ids"][0]
    workflow_memory = client.post(
        "/api/v0/memory-items",
        json={
            "title": "Release calendar",
            "text": "The release checklist owner is Maya.",
            "scope": "workflow",
            "scope_ref": workflow_id,
        },
    )
    assert workflow_memory.status_code == 201
    first = client.post(
        "/api/v0/threads",
        json={"agent_id": agent_id, "title": "Release conversation A"},
    )
    second = client.post(
        "/api/v0/threads",
        json={"agent_id": agent_id, "title": "Release conversation B"},
    )
    assert first.status_code == second.status_code == 201
    thread_memory = client.post(
        "/api/v0/memory-items",
        json={
            "title": "Conversation decision",
            "text": "Conversation A approved the blue release window.",
            "scope": "thread",
            "scope_ref": first.json()["thread_id"],
        },
    )
    assert thread_memory.status_code == 201
    return workflow_memory.json(), first.json(), second.json(), thread_memory.json()


def _run_in_separate_memory_scopes(client, scope):
    """Run the same query in two threads and verify scoped retrieval results."""
    workflow_memory, first_thread, second_thread, thread_memory = scope
    task = "Who owns the release checklist and what is the blue window?"
    first = client.post(
        "/api/v0/runs",
        json={"task": task, "thread_id": first_thread["thread_id"]},
    )
    second = client.post(
        "/api/v0/runs",
        json={"task": task, "thread_id": second_thread["thread_id"]},
    )
    assert first.status_code == second.status_code == 201
    first_sources = first.json()["agent_run"]["output_summary"]["source_ids"]
    second_sources = second.json()["agent_run"]["output_summary"]["source_ids"]
    thread_id = f"memory:{thread_memory['memory_id']}"
    workflow_id = f"memory:{workflow_memory['memory_id']}"
    assert thread_id in first_sources and workflow_id in first_sources
    assert thread_id not in second_sources and workflow_id in second_sources
    assert first.json()["run"]["trigger_ref"] == first_thread["thread_id"]
    return first.json()


def _assert_thread_run_history(client, scope, run):
    """Check thread history points to the run created in that thread."""
    _, first_thread, _, _ = scope
    response = client.get(f"/api/v0/threads/{first_thread['thread_id']}/runs")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["run_id"] == run["run"]["run_id"]


def _assert_closed_thread_rejects_runs(client, scope):
    """Close a conversation and verify new execution requests are rejected."""
    _, first_thread, _, _ = scope
    closed = client.patch(
        f"/api/v0/threads/{first_thread['thread_id']}",
        json={"status": "closed"},
    )
    assert closed.status_code == 200
    rejected = client.post(
        "/api/v0/runs",
        json={
            "task": "Do not continue a closed conversation.",
            "thread_id": first_thread["thread_id"],
        },
    )
    assert rejected.status_code == 422
