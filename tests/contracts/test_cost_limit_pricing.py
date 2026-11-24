"""A cost limit is only enforceable on a priced model, and that is caught early.

Publishing an agent, or setting a model profile's cost limit or prices, rejects a cost
limit on an unpriced model with an actionable 422. Agent versions written before that
check existed fail their task turn with the same actionable message instead of a generic
one, and the operator log keeps the cause.
"""

import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import TaskTurnStatus
from modules.agents.runtime_state import (
    AgentRuntimeBudgetExceeded,
    RunCostBudgetExceeded,
)
from modules.models.pricing import (
    COST_LIMIT_NEEDS_PRICING,
    PRICING_INCOMPLETE,
    ensure_cost_limits_are_enforceable,
    ensure_pricing_is_complete,
    is_priced,
)
from modules.tasks.turn_failures import (
    COST_LIMIT_CODE,
    GENERIC_FAILURE_CODE,
    RUNTIME_LIMIT_CODE,
    turn_failure_summary,
)
from shared.errors import ConfigurationRejected, ModelGatewayError
from tests.support.runtime import LocalPlatformRuntime
from tests.support.task_execution import run_task

PRICES = {
    "input_cost_per_million_tokens_usd": "3",
    "output_cost_per_million_tokens_usd": "15",
}


def _local_profile():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    return runtime.model_profiles.get("local-model-profile")


def _priced(profile, input_rate="3", output_rate="15"):
    return profile.model_copy(
        update={
            "input_cost_per_million_tokens_usd": Decimal(input_rate),
            "output_cost_per_million_tokens_usd": Decimal(output_rate),
        }
    )


# ---------- Pricing rules ----------


def test_a_profile_is_priced_only_with_both_token_prices():
    profile = _local_profile()
    assert not is_priced(profile)
    assert is_priced(_priced(profile))
    half = profile.model_copy(
        update={"input_cost_per_million_tokens_usd": Decimal("3")}
    )
    assert not is_priced(half)
    with pytest.raises(ConfigurationRejected) as rejected:
        ensure_pricing_is_complete(half)
    assert rejected.value.code == PRICING_INCOMPLETE


def test_cost_limits_need_a_priced_model():
    profile = _local_profile()
    ensure_cost_limits_are_enforceable(profile)  # no cost limit: nothing to enforce
    with pytest.raises(ConfigurationRejected) as agent_limit:
        ensure_cost_limits_are_enforceable(profile, Decimal("10"))
    assert agent_limit.value.code == COST_LIMIT_NEEDS_PRICING
    assert "remove the agent's cost limit" in agent_limit.value.message
    capped_profile = profile.model_copy(update={"cost_budget_usd": Decimal("5")})
    with pytest.raises(ConfigurationRejected) as profile_limit:
        ensure_cost_limits_are_enforceable(capped_profile)
    assert "remove its cost limit" in profile_limit.value.message
    ensure_cost_limits_are_enforceable(_priced(capped_profile), Decimal("10"))


# ---------- Publish time ----------


def test_publishing_an_agent_with_a_cost_limit_on_an_unpriced_model_is_rejected():
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        rejected = client.post(
            "/api/v0/agents",
            json={
                "name": "Capped assistant",
                "instructions": "Be concise.",
                "max_run_cost_usd": "10",
            },
        )
        assert rejected.status_code == 422
        assert "has no token prices" in rejected.json()["detail"]
        assert not [
            agent for agent in runtime.agents.list() if agent.name == "Capped assistant"
        ]

        priced = client.patch("/api/v0/model-profiles/local-model-profile", json=PRICES)
        assert priced.status_code == 200
        accepted = client.post(
            "/api/v0/agents",
            json={
                "name": "Capped assistant",
                "instructions": "Be concise.",
                "max_run_cost_usd": "10",
            },
        )
        assert accepted.status_code == 201, accepted.text


def test_adding_a_cost_limit_to_an_agent_on_an_unpriced_model_is_rejected():
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        agent = client.post(
            "/api/v0/agents",
            json={"name": "Plain assistant", "instructions": "Be concise."},
        ).json()
        version_before = agent["current_version_id"]
        budget = {
            "max_runtime_seconds": 300,
            "max_tool_calls": 10,
            "max_output_tokens": 4096,
            "max_run_cost_usd": "10",
        }
        rejected = client.patch(
            f"/api/v0/agents/{agent['id']}", json={"budget": budget}
        )
        assert rejected.status_code == 422
        assert "has no token prices" in rejected.json()["detail"]
        # No new version was published.
        current = client.get(f"/api/v0/agents/{agent['id']}").json()
        assert current["current_version_id"] == version_before

        # Once the model is priced the same edit publishes, with the limit as a JSON
        # number (what the builder sends) or a string.
        assert (
            client.patch(
                "/api/v0/model-profiles/local-model-profile", json=PRICES
            ).status_code
            == 200
        )
        for limit in (10, "12.5"):
            edited = client.patch(
                f"/api/v0/agents/{agent['id']}",
                json={"budget": {**budget, "max_run_cost_usd": limit}},
            )
            assert edited.status_code == 200, edited.text
            assert Decimal(
                str(edited.json()["current_version"]["budget"]["max_run_cost_usd"])
            ) == Decimal(str(limit))


def test_model_profile_cost_limits_and_prices_are_validated_and_audited():
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        path = "/api/v0/model-profiles/local-model-profile"
        unpriced_limit = client.patch(path, json={"cost_budget_usd": "5"})
        assert unpriced_limit.status_code == 422
        assert "no token prices" in unpriced_limit.json()["detail"]
        half_priced = client.patch(
            path, json={"input_cost_per_million_tokens_usd": "3"}
        )
        assert half_priced.status_code == 422
        assert "both the input and output" in half_priced.json()["detail"]

        accepted = client.patch(path, json={**PRICES, "cost_budget_usd": "5"})
        assert accepted.status_code == 200, accepted.text
        event = [
            item
            for item in runtime.audit_events.list()
            if item.action == "model_profile.updated"
        ][-1]
        assert list(event.metadata["changed_fields"]) == [
            "cost_budget_usd",
            "input_cost_per_million_tokens_usd",
            "output_cost_per_million_tokens_usd",
        ]
        # Unrelated edits are not blocked by the pricing checks.
        assert client.patch(path, json={"max_output_tokens": 512}).status_code == 200


# ---------- Run time (versions published before the check) ----------


def _legacy_capped_agent(client, runtime):
    """An agent version carrying a cost limit, written as older releases allowed."""
    agent = client.post(
        "/api/v0/agents",
        json={"name": "Legacy capped", "instructions": "Be concise."},
    ).json()
    version = runtime.agent_versions.get(agent["current_version_id"])
    runtime.agent_versions.put(
        version.model_copy(
            update={
                "budget": version.budget.model_copy(
                    update={"max_run_cost_usd": Decimal("10")}
                )
            }
        )
    )
    return agent


def test_a_legacy_capped_agent_fails_its_turn_with_an_actionable_message(caplog):
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        agent = _legacy_capped_agent(client, runtime)
        with caplog.at_level(logging.WARNING, logger="modules.tasks.worker_service"):
            task, turn, _run, _artifacts = run_task(
                client, runtime, "Summarize", agent_id=agent["id"]
            )
        # The task carries the structured error, so clients can offer the right step.
        latest = client.get(f"/api/v0/tasks/{task['task_id']}").json()
        assert latest["latest_turn_error"]["code"] == COST_LIMIT_NEEDS_PRICING
        assert latest["latest_turn_error"]["retryable"] is False
        # A retry would run the same agent version and fail the same way.
        retry = client.post(
            f"/api/v0/tasks/{task['task_id']}/turns/{turn.turn_id}/retry"
        )
        assert retry.status_code == 409
        assert "start a new task" in retry.json()["detail"]

    assert turn.status.value == "failed"
    assert turn.error.code == COST_LIMIT_NEEDS_PRICING
    assert turn.error.retryable is False
    assert "has no token prices" in turn.error.summary
    assert "start a new task" in turn.error.summary
    logged = [record for record in caplog.records if record.msg == "task turn failed"]
    assert logged and logged[-1].exc_info is None  # expected outcome: no stack


def test_an_unexpected_failure_stays_generic_for_the_owner_and_keeps_its_stack(
    caplog, monkeypatch
):
    runtime = LocalPlatformRuntime()

    def fail(*_args, **_kwargs):
        raise RuntimeError("internal detail that must not reach the owner")

    with TestClient(create_app(runtime)) as client:
        monkeypatch.setattr(
            runtime.task_worker_service.agent_execution, "run_agent", fail
        )
        with caplog.at_level(logging.WARNING, logger="modules.tasks.worker_service"):
            _task, turn, _run, _artifacts = run_task(client, runtime, "Summarize")

    assert turn.error.code == GENERIC_FAILURE_CODE
    assert "internal detail" not in turn.error.summary
    assert turn.error.retryable is True  # unexpected failures stay retryable
    logged = [record for record in caplog.records if record.msg == "task turn failed"]
    assert logged and logged[-1].exc_info is not None


# ---------- What the task owner is told ----------


@pytest.mark.parametrize(
    ("error", "code", "fragment", "retryable"),
    [
        (
            ConfigurationRejected(COST_LIMIT_NEEDS_PRICING, "Set prices."),
            COST_LIMIT_NEEDS_PRICING,
            "start a new task",
            False,
        ),
        (
            AgentRuntimeBudgetExceeded("internal", limit_seconds=3),
            RUNTIME_LIMIT_CODE,
            "3-second runtime limit",
            False,
        ),
        (RunCostBudgetExceeded("internal"), COST_LIMIT_CODE, "cost limit", False),
        (
            ModelGatewayError(
                "model.timeout", "Model provider request timed out", True
            ),
            "model.timeout",
            "timed out",
            True,
        ),
        (RuntimeError("boom"), GENERIC_FAILURE_CODE, "could not complete", True),
        (
            ModelGatewayError("model.bad", "api_key=sk-should-never-show"),
            GENERIC_FAILURE_CODE,
            "could not complete",
            True,
        ),
    ],
)
def test_turn_failure_summary_is_specific_only_for_known_safe_errors(
    error, code, fragment, retryable
):
    summary = turn_failure_summary(error)
    assert summary.code == code
    assert fragment in summary.summary
    assert summary.retryable is retryable


def test_a_turn_whose_worker_stopped_can_still_be_retried():
    """Only failures that need a change first refuse a retry; a lost worker does not."""
    runtime = LocalPlatformRuntime()
    with TestClient(create_app(runtime)) as client:
        task = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "local-assistant",
                "title": "Lease",
                "initial_message": "Summarize",
            },
        ).json()
        turn = runtime.task_turns.get(task["latest_turn_id"])
        long_ago = turn.created_at - timedelta(hours=1)
        runtime.task_turns.put(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.RUNNING,
                    "attempts": 3,
                    "worker_id": "lost-worker",
                    "started_at": long_ago,
                    "updated_at": long_ago,
                }
            )
        )
        stored = runtime.tasks.get(task["task_id"])
        runtime.tasks.put(
            stored.model_copy(
                update={
                    "active_turn_status": TaskTurnStatus.RUNNING,
                    "latest_turn_status": TaskTurnStatus.RUNNING,
                }
            )
        )
        runtime.task_worker_service.recover_stale_turns(stale_after_seconds=60)

        failed = runtime.task_turns.get(turn.turn_id)
        assert failed.error.code == "task.worker_lease_expired"
        latest = client.get(f"/api/v0/tasks/{task['task_id']}").json()
        assert latest["latest_turn_error"]["code"] == "task.worker_lease_expired"
        retry = client.post(
            f"/api/v0/tasks/{task['task_id']}/turns/{turn.turn_id}/retry"
        )
        assert retry.status_code == 202, retry.text
