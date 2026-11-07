from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.app import create_app
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
        assert client.get("/api/v0/knowledge-documents").status_code == 404
        assert client.get("/api/v0/memory-items").status_code == 404
















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
