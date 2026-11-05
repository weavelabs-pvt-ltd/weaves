from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


def test_run_idempotency_replays_success_and_rejects_key_reuse():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    headers = {"Idempotency-Key": "client-request-001"}
    payload = {
        "agent_id": "local-assistant",
        "title": "Idempotency task",
        "initial_message": "Summarize the security basics",
    }

    with client:
        first = client.post(
            "/api/v0/tasks",
            json=payload,
            headers=headers,
        )
        assert first.status_code == 202

        repeated = client.post(
            "/api/v0/tasks",
            json=payload,
            headers=headers,
        )
        assert repeated.status_code == 202
        assert repeated.json()["task_id"] == first.json()["task_id"]
        assert len(runtime.tasks.list()) == 1

        conflict = client.post(
            "/api/v0/tasks",
            json={**payload, "initial_message": "Summarize the time off policy"},
            headers=headers,
        )
        assert conflict.status_code == 409


def test_published_workflow_exposes_its_versioned_graph():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        workflows_response = client.get("/api/v0/workflows")
        assert workflows_response.status_code == 200
        workflow_id = workflows_response.json()[0]["workflow_id"]

        detail = client.get(f"/api/v0/workflows/{workflow_id}").json()
        graph = detail["graph"]
        assert graph["workflow_id"] == workflow_id
        assert len(graph["nodes"]) == 1
        assert (
            graph["nodes"][0]["agent_version_id"]
            == (detail["version"]["agent_version_ids"][0])
        )


def test_api_rejects_unknown_workflow_run():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        response = client.post(
            "/api/v0/workflows/missing-workflow/runs",
            json={"instructions": "Summarize the security basics"},
        )

    assert response.status_code == 404


def test_api_creates_manages_and_keeps_workflows_bound_to_agent_versions():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        context = _create_version_bound_workflow(client)
        _update_workflow_agent_and_check_history(client, context)
        _assert_archived_workflow_can_be_reactivated(client, context["workflow_id"])


def _create_version_bound_workflow(client):
    """Create a workflow pinned to an immutable agent version."""
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
        json={"instructions": "Review the next release"},
    )
    assert unavailable.status_code == 422
    reactivated = client.patch(
        f"/api/v0/workflows/{workflow_id}", json={"status": "active"}
    )
    assert reactivated.status_code == 200


def test_linear_workflow_projects_ordered_steps_into_graph():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))

    with client:
        agent_ids, workflow_id, _version = _create_linear_workflow(client)
        graph = client.get(f"/api/v0/workflows/{workflow_id}").json()["graph"]
        assert [node["agent_id"] for node in graph["nodes"]] == agent_ids
        assert [stage["execution_mode"] for stage in graph["stages"]] == [
            "sequential",
            "sequential",
        ]


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
