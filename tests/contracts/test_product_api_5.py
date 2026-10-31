from fastapi.testclient import TestClient

from apps.api.app import create_app
from clients.model_providers.gateway import ModelGateway
from contracts.v1 import (
    ModelProviderType,
    ModelResponse,
    ModelUsage,
)
from tests.support.runtime import LocalPlatformRuntime


def test_run_idempotency_replays_success_and_rejects_key_reuse():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    headers = {"Idempotency-Key": "client-request-001"}

    with client:
        first = client.post(
            "/api/v0/runs",
            json={"task": "Summarize the security basics"},
            headers=headers,
        )
        assert first.status_code == 201
        assert first.json()["replayed"] is False

        repeated = client.post(
            "/api/v0/runs",
            json={"task": "Summarize the security basics"},
            headers=headers,
        )
        assert repeated.status_code == 201
        assert repeated.json()["replayed"] is True
        assert repeated.json()["run"]["run_id"] == first.json()["run"]["run_id"]
        assert len(client.get("/api/v0/runs").json()) == 1
        assert len(runtime.tool_invocations.list()) == 1

        conflict = client.post(
            "/api/v0/runs",
            json={"task": "Summarize the time off policy"},
            headers=headers,
        )
        assert conflict.status_code == 409


def test_api_runs_published_workflow_by_workflow_id():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        workflows_response = client.get("/api/v0/workflows")
        assert workflows_response.status_code == 200
        workflow_id = workflows_response.json()[0]["workflow_id"]

        response = client.post(
            f"/api/v0/workflows/{workflow_id}/runs",
            json={"task": "Summarize the security basics"},
            headers={"Idempotency-Key": "workflow-run-001"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["run"]["workflow_id"] == workflow_id
        assert body["run"]["status"] == "succeeded"
        assert body["artifacts"][0]["provenance"]
        assert body["replayed"] is False

        replayed = client.post(
            f"/api/v0/workflows/{workflow_id}/runs",
            json={"task": "Summarize the security basics"},
            headers={"Idempotency-Key": "workflow-run-001"},
        )
        assert replayed.status_code == 201
        assert replayed.json()["replayed"] is True
        assert replayed.json()["run"]["run_id"] == body["run"]["run_id"]

        newer = client.post(
            f"/api/v0/workflows/{workflow_id}/runs",
            json={"task": "Summarize the time off policy"},
        )
        assert newer.status_code == 201
        _create_other_workflow_run(client)
        _assert_workflow_run_history(
            client,
            workflow_id,
            body["run"]["run_id"],
            newer.json()["run"]["run_id"],
        )


def _create_other_workflow_run(client: TestClient) -> None:
    agent = client.post(
        "/api/v0/agents",
        json={"name": "Separate workflow agent", "instructions": "Summarize."},
    )
    assert agent.status_code == 201
    workflow = client.post(
        "/api/v0/workflows",
        json={"name": "Separate workflow", "agent_id": agent.json()["id"]},
    )
    assert workflow.status_code == 201
    run = client.post(
        f"/api/v0/workflows/{workflow.json()['definition']['workflow_id']}/runs",
        json={"task": "Summarize a separate process"},
    )
    assert run.status_code == 201


def _assert_workflow_run_history(
    client: TestClient,
    workflow_id: str,
    original_run_id: str,
    newer_run_id: str,
) -> None:
    history = client.get(f"/api/v0/workflows/{workflow_id}/runs")
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert {item["run_id"] for item in items} == {newer_run_id, original_run_id}
    assert all(item["workflow_id"] == workflow_id for item in items)
    assert items == sorted(
        items,
        key=lambda item: (item["created_at"], item["run_id"]),
        reverse=True,
    )
    assert items[0]["agent_runs"]
    assert items[0]["artifacts"]
    assert items[0]["tool_invocations"]
    _assert_history_pagination(client, workflow_id, items)


def _assert_history_pagination(
    client: TestClient, workflow_id: str, history_items: list[dict]
) -> None:
    first_page = client.get(
        f"/api/v0/workflows/{workflow_id}/runs", params={"limit": 1}
    ).json()
    assert [item["run_id"] for item in first_page["items"]] == [
        history_items[0]["run_id"]
    ]
    assert first_page["next_cursor"]
    second_page = client.get(
        f"/api/v0/workflows/{workflow_id}/runs",
        params={"limit": 1, "cursor": first_page["next_cursor"]},
    ).json()
    assert [item["run_id"] for item in second_page["items"]] == [
        history_items[1]["run_id"]
    ]
    assert second_page["next_cursor"] is None


def test_api_rejects_unknown_workflow_run():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        response = client.post(
            "/api/v0/workflows/missing-workflow/runs",
            json={"task": "Summarize the security basics"},
        )

    assert response.status_code == 404


def test_api_creates_manages_and_keeps_workflows_bound_to_agent_versions():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        context = _create_version_bound_workflow(client)
        _update_workflow_agent_and_check_history(client, context)
        _assert_archived_workflow_can_be_reactivated(client, context["workflow_id"])


def _create_version_bound_workflow(client):
    """Create and run a workflow pinned to an immutable agent version."""
    agent_response = client.post(
        "/api/v0/agents",
        json={"name": "Release helper", "instructions": "Review releases."},
    )
    assert agent_response.status_code == 201
    agent = agent_response.json()
    created = client.post(
        "/api/v0/workflows",
        json={
            "name": "Release risk review",
            "description": "Review an upcoming release.",
            "agent_id": agent["id"],
        },
    )
    assert created.status_code == 201
    workflow = created.json()["definition"]
    workflow_id = workflow["workflow_id"]
    version = created.json()["version"]
    first_workflow_version_id = version["workflow_version_id"]
    agent_version_id = version["agent_version_ids"][0]
    assert workflow["status"] == "active"
    generated = next(
        item
        for item in client.get("/api/v0/workflows").json()
        if item["name"] == "Run Release helper"
    )
    generated_version = client.get(
        f"/api/v0/workflows/{generated['workflow_id']}"
    ).json()["version"]
    assert generated_version["version"] == 2
    assert generated_version["agent_version_ids"] == [agent_version_id]
    detail = client.get(f"/api/v0/workflows/{workflow_id}")
    assert detail.status_code == 200
    assert (
        detail.json()["version"]["workflow_version_id"]
        == workflow["current_version_id"]
    )
    first_run = client.post(
        f"/api/v0/workflows/{workflow_id}/runs",
        json={"task": "Review the release notes"},
    )
    assert first_run.status_code == 201
    assert first_run.json()["run"]["workflow_id"] == workflow_id
    assert first_run.json()["run"]["workflow_version_id"] == first_workflow_version_id
    assert first_run.json()["agent_run"]["agent_version_id"] == agent_version_id
    return {
        "agent": agent,
        "workflow_id": workflow_id,
        "workflow_version_id": first_workflow_version_id,
        "agent_version_id": agent_version_id,
        "generated_workflow_id": generated["workflow_id"],
    }


def _update_workflow_agent_and_check_history(client, context):
    """Publish a new agent version and verify linked workflow version history."""
    agent = context["agent"]
    workflow_id = context["workflow_id"]
    updated = client.patch(
        f"/api/v0/agents/{agent['id']}",
        json={"instructions": "Review releases and identify risks."},
    )
    assert updated.status_code == 200
    new_agent_version_id = updated.json()["current_version"]["agent_version_id"]
    workflow = client.get(f"/api/v0/workflows/{workflow_id}").json()
    assert workflow["version"]["version"] == 2
    assert workflow["version"]["agent_version_ids"] == [new_agent_version_id]
    page = client.get(f"/api/v0/workflows/{workflow_id}/versions", params={"limit": 1})
    assert page.status_code == 200, page.text
    assert [item["version"] for item in page.json()["items"]] == [2]
    assert page.json()["next_before_version"] == 2
    history = client.get(
        f"/api/v0/workflows/{workflow_id}/versions",
        params={"limit": 1, "before_version": 2},
    )
    assert [item["version"] for item in history.json()["items"]] == [1]
    assert history.json()["next_before_version"] is None
    historical = client.get(
        f"/api/v0/workflows/{workflow_id}/versions/{context['workflow_version_id']}"
    )
    assert historical.status_code == 200, historical.text
    assert historical.json()["agent_version_ids"] == [context["agent_version_id"]]
    generated = client.get(
        f"/api/v0/workflows/{context['generated_workflow_id']}"
    ).json()["version"]
    assert generated["version"] == 3
    assert generated["agent_version_ids"] == [new_agent_version_id]
    cross_workflow = client.get(
        f"/api/v0/workflows/{workflow_id}/versions/{generated['workflow_version_id']}"
    )
    assert cross_workflow.status_code == 404


def _assert_archived_workflow_can_be_reactivated(client, workflow_id):
    """Reject runs while archived and allow execution after reactivation."""
    archived = client.patch(
        f"/api/v0/workflows/{workflow_id}", json={"status": "archived"}
    )
    assert archived.status_code == 200
    unavailable = client.post(
        f"/api/v0/workflows/{workflow_id}/runs",
        json={"task": "Review the next release"},
    )
    assert unavailable.status_code == 422
    reactivated = client.patch(
        f"/api/v0/workflows/{workflow_id}", json={"status": "active"}
    )
    assert reactivated.status_code == 200
    run = client.post(
        f"/api/v0/workflows/{workflow_id}/runs",
        json={"task": "Review the next release"},
    )
    assert run.status_code == 201
    assert run.json()["run"]["status"] == "succeeded"


def test_api_runs_linear_multi_agent_workflow_and_replays_all_steps():
    runtime = LocalPlatformRuntime()
    captured_requests = []

    class CapturingModel:
        def complete(self, request, binding):
            captured_requests.append(request)
            step = len(captured_requests)
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content=f"Step {step} output",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = CapturingModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)
    client = TestClient(create_app(runtime))

    with client:
        agent_ids, workflow_id, version = _create_linear_workflow(client)
        _assert_linear_workflow_replay(
            client, workflow_id, agent_ids, captured_requests
        )
        _assert_linear_workflow_republishes_agent_version(
            client, workflow_id, agent_ids, version, captured_requests
        )


def _create_linear_workflow(client):
    """Create two version-pinned workflow agents and return their published plan."""
    agent_ids = []
    for name in ("Planner", "Reviewer"):
        response = client.post(
            "/api/v0/agents",
            json={"name": name, "instructions": f"Act as the {name.lower()}."},
        )
        assert response.status_code == 201
        agent_ids.append(response.json()["id"])
    created = client.post(
        "/api/v0/workflows",
        json={"name": "Plan then review", "agent_ids": agent_ids},
    )
    assert created.status_code == 201
    version = created.json()["version"]
    assert version["handler_key"] == "agent.sequence"
    assert len(version["agent_version_ids"]) == 2
    return agent_ids, created.json()["definition"]["workflow_id"], version


def _assert_linear_workflow_replay(client, workflow_id, agent_ids, captured_requests):
    """Run both workflow steps, inspect artifacts, and verify idempotent replay."""
    headers = {"Idempotency-Key": "linear-workflow-001"}
    response = client.post(
        f"/api/v0/workflows/{workflow_id}/runs",
        json={"task": "Assess this release candidate"},
        headers=headers,
    )
    assert response.status_code == 201
    result = response.json()
    run_id = result["run"]["run_id"]
    assert result["run"]["status"] == "succeeded"
    assert result["run"]["workflow_id"] == workflow_id
    assert [item["agent_id"] for item in result["agent_runs"]] == agent_ids
    assert [
        item["input_summary"]["workflow_step_index"] for item in result["agent_runs"]
    ] == [0, 1]
    assert len(result["artifacts"]) == 2
    assert all(item["run_id"] == run_id for item in result["agent_runs"])
    assert all(item["run_id"] == run_id for item in result["artifacts"])
    assert any(
        item["source_ref"] == result["artifacts"][0]["artifact_id"]
        for item in result["artifacts"][1]["provenance"]
    )
    assert "Step 1 output" in captured_requests[1].messages[-1].content
    assert "untrusted reference data" in captured_requests[1].messages[0].content
    detail = client.get(f"/api/v0/runs/{run_id}")
    assert len(detail.json()["agent_runs"]) == 2
    assert len(detail.json()["artifacts"]) == 2
    replay = client.post(
        f"/api/v0/workflows/{workflow_id}/runs",
        json={"task": "Assess this release candidate"},
        headers=headers,
    )
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["run"]["run_id"] == run_id
    assert len(replay.json()["agent_runs"]) == 2
    assert len(captured_requests) == 2


def _assert_linear_workflow_republishes_agent_version(
    client, workflow_id, agent_ids, version, captured_requests
):
    """Publish an agent update and confirm a new workflow run uses it."""
    updated = client.patch(
        f"/api/v0/agents/{agent_ids[0]}",
        json={"instructions": "Plan the release review with clear criteria."},
    )
    assert updated.status_code == 200
    workflow = client.get(f"/api/v0/workflows/{workflow_id}").json()
    assert workflow["version"]["version"] == 2
    assert workflow["version"]["agent_version_ids"] == [
        updated.json()["current_version"]["agent_version_id"],
        version["agent_version_ids"][1],
    ]
    response = client.post(
        f"/api/v0/workflows/{workflow_id}/runs",
        json={"task": "Assess this release candidate"},
        headers={"Idempotency-Key": "linear-workflow-002"},
    )
    assert response.status_code == 201
    assert [item["agent_id"] for item in response.json()["agent_runs"]] == agent_ids
    assert len(captured_requests) == 4
