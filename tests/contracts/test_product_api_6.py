import json
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
)
from tests.support.runtime import LocalPlatformRuntime


def test_api_replaces_workflow_steps_by_publishing_new_versions():
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        agent_ids = []
        for name in ("Initial", "Reviewer", "Escalation"):
            response = client.post(
                "/api/v0/agents",
                json={"name": name, "instructions": f"Act as {name}."},
            )
            assert response.status_code == 201, response.text
            agent_ids.append(response.json()["id"])

        first = client.post(
            "/api/v0/workflows",
            json={"name": "Initial review", "agent_id": agent_ids[0]},
        )
        second = client.post(
            "/api/v0/workflows",
            json={"name": "Escalation review", "agent_id": agent_ids[2]},
        )
        assert first.status_code == second.status_code == 201
        first_id = first.json()["definition"]["workflow_id"]
        second_id = second.json()["definition"]["workflow_id"]
        previous_first_version = first.json()["version"]["workflow_version_id"]
        previous_second_version = second.json()["version"]["workflow_version_id"]

        replaced = client.put(
            f"/api/v0/workflows/{first_id}/steps",
            json={"agent_ids": [agent_ids[1], agent_ids[2]]},
        )
        assert replaced.status_code == 200, replaced.text
        current = replaced.json()["version"]
        assert current["version"] == 2
        assert current["handler_key"] == "agent.sequence"
        assert [
            client.get(f"/api/v0/agents/{agent_id}").json()["current_version"][
                "agent_version_id"
            ]
            for agent_id in agent_ids[1:]
        ] == current["agent_version_ids"]

        # Publishing a new agent version also advances other workflows that
        # referenced the prior version, without changing their step order.
        changed_second = client.get(f"/api/v0/workflows/{second_id}").json()
        assert changed_second["version"]["version"] == 2
        assert changed_second["version"]["workflow_version_id"] != (
            previous_second_version
        )
        assert changed_second["version"]["agent_version_ids"] == [
            current["agent_version_ids"][1]
        ]
        assert previous_first_version != current["workflow_version_id"]

        graph = replaced.json()["graph"]
        assert [item["agent_id"] for item in graph["nodes"]] == agent_ids[1:]
        assert [item["step_index"] for item in graph["nodes"]] == [0, 1]


def test_api_publishes_workflow_conditions_as_graph_edges():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        agent_ids = _create_conditional_workflow_agents(client)
        workflow_id, published = _publish_conditional_workflow(client, agent_ids)
        _assert_conditional_workflow_graph(client, workflow_id, agent_ids)
        _assert_invalid_conditional_versions_are_not_published(
            client, workflow_id, agent_ids, published
        )


def _create_conditional_workflow_agents(client):
    """Create the router and three conditional workflow steps."""
    names = (
        "API Router",
        "Conditional Reviewer",
        "Conditional Escalation",
        "API Summary",
    )
    agent_ids = []
    for name in names:
        payload = {
            "name": name,
            "instructions": f"Act as {name}.",
            "allowed_capability_ids": [],
        }
        if name == "API Router":
            payload["output_schema"] = {
                "type": "object",
                "properties": {"route": {"type": "string"}},
                "required": ["route"],
                "additionalProperties": False,
            }
        response = client.post("/api/v0/agents", json=payload)
        assert response.status_code == 201, response.text
        agent_ids.append(response.json()["id"])
    return agent_ids


def _publish_conditional_workflow(client, agent_ids):
    """Publish the route conditions and any-branch summary dependency."""
    created = client.post(
        "/api/v0/workflows",
        json={"name": "Conditional API workflow", "agent_ids": agent_ids},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["definition"]["workflow_id"]
    payload = {
        "agent_ids": agent_ids,
        "step_conditions": [
            {
                "source_agent_id": agent_ids[0],
                "target_agent_id": agent_ids[1],
                "json_pointer": "/route",
                "operator": "equals",
                "expected_value": "review",
            },
            {
                "source_agent_id": agent_ids[0],
                "target_agent_id": agent_ids[2],
                "json_pointer": "/route",
                "operator": "equals",
                "expected_value": "escalate",
            },
        ],
        "step_dependencies": [
            {"target_agent_id": agent_ids[1], "depends_on_agent_ids": [agent_ids[0]]},
            {"target_agent_id": agent_ids[2], "depends_on_agent_ids": [agent_ids[0]]},
            {
                "target_agent_id": agent_ids[3],
                "depends_on_agent_ids": agent_ids[1:3],
                "join_policy": "any",
            },
        ],
    }
    published = client.put(f"/api/v0/workflows/{workflow_id}/steps", json=payload)
    assert published.status_code == 200, published.text
    assert [
        item["target_agent_id"]
        for item in published.json()["version"]["step_conditions"]
    ] == agent_ids[1:3]
    assert published.json()["version"]["step_dependencies"][2]["join_policy"] == "any"
    return workflow_id, published.json()


def _assert_conditional_workflow_graph(client, workflow_id, agent_ids):
    """Verify conditions and their links are exposed in the graph projection."""
    detail = client.get(f"/api/v0/workflows/{workflow_id}").json()
    response = client.get(
        f"/api/v0/workflows/{workflow_id}/versions/"
        f"{detail['version']['workflow_version_id']}/graph"
    )
    assert response.status_code == 200
    graph = response.json()["graph"]
    assert [node["agent_id"] for node in graph["nodes"]] == agent_ids
    conditional_edges = [edge for edge in graph["edges"] if edge["condition"]]
    assert [edge["condition"]["expected_value"] for edge in conditional_edges] == [
        "review",
        "escalate",
    ]


def _assert_invalid_conditional_versions_are_not_published(
    client, workflow_id, agent_ids, published
):
    """Reject cyclic and backward condition graphs without changing version."""
    version_id = published["version"]["workflow_version_id"]
    invalid = client.put(
        f"/api/v0/workflows/{workflow_id}/steps",
        json={
            "agent_ids": agent_ids,
            "step_conditions": [
                {
                    "source_agent_id": agent_ids[1],
                    "target_agent_id": agent_ids[0],
                    "json_pointer": "/route",
                    "operator": "exists",
                }
            ],
        },
    )
    assert invalid.status_code == 422
    current = client.get(f"/api/v0/workflows/{workflow_id}").json()
    assert current["version"]["workflow_version_id"] == version_id
    invalid_graph = client.put(
        f"/api/v0/workflows/{workflow_id}/steps",
        json={
            "agent_ids": agent_ids,
            "step_dependencies": [
                {
                    "target_agent_id": agent_ids[1],
                    "depends_on_agent_ids": [agent_ids[2]],
                },
                {
                    "target_agent_id": agent_ids[2],
                    "depends_on_agent_ids": [agent_ids[0]],
                },
                {
                    "target_agent_id": agent_ids[3],
                    "depends_on_agent_ids": agent_ids[1:3],
                    "join_policy": "any",
                },
            ],
        },
    )
    assert invalid_graph.status_code == 422
    current = client.get(f"/api/v0/workflows/{workflow_id}").json()
    assert current["version"]["workflow_version_id"] == version_id


def test_api_projects_versioned_parallel_workflow_stages():
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        agent_ids, workflow_id, published = _create_parallel_workflow(client)
        current_version = _assert_parallel_group_persists_after_agent_update(
            client, agent_ids, workflow_id, published
        )
        _assert_parallel_workflow_graph(client, workflow_id, agent_ids)
        _assert_invalid_parallel_edit_is_not_published(
            client, workflow_id, agent_ids, current_version
        )


def _create_parallel_workflow(client):
    """Create four agents and publish the parallel group plus join step."""
    names = ("Parallel root", "Parallel reviewer", "Parallel risk", "Join")
    agent_ids = []
    for name in names:
        response = client.post(
            "/api/v0/agents",
            json={
                "name": name,
                "instructions": f"Complete the {name} step.",
                "allowed_capability_ids": [],
            },
        )
        assert response.status_code == 201, response.text
        agent_ids.append(response.json()["id"])
    created = client.post(
        "/api/v0/workflows",
        json={"name": "Parallel workflow", "agent_ids": agent_ids},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["definition"]["workflow_id"]
    published = client.put(
        f"/api/v0/workflows/{workflow_id}/steps",
        json={
            "agent_ids": agent_ids,
            "parallel_groups": [{"agent_ids": agent_ids[1:3]}],
            "step_dependencies": [
                {
                    "target_agent_id": agent_ids[2],
                    "depends_on_agent_ids": [agent_ids[0]],
                },
                {
                    "target_agent_id": agent_ids[3],
                    "depends_on_agent_ids": agent_ids[1:3],
                    "join_policy": "all",
                },
            ],
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["version"]["parallel_groups"] == [
        {"schema_version": 1, "agent_ids": agent_ids[1:3]}
    ]
    return agent_ids, workflow_id, published.json()


def _assert_parallel_group_persists_after_agent_update(
    client, agent_ids, workflow_id, published
):
    """Update the root agent and verify the workflow keeps parallel metadata."""
    updated = client.patch(
        f"/api/v0/agents/{agent_ids[0]}",
        json={"instructions": "Complete the intake step carefully."},
    )
    assert updated.status_code == 200, updated.text
    current = client.get(f"/api/v0/workflows/{workflow_id}").json()["version"]
    assert current["parallel_groups"] == published["version"]["parallel_groups"]
    assert current["step_dependencies"] == published["version"]["step_dependencies"]
    return current


def _assert_parallel_workflow_graph(client, workflow_id, agent_ids):
    """Verify parallel stage and join dependency data are in the graph."""
    detail = client.get(f"/api/v0/workflows/{workflow_id}").json()
    response = client.get(
        f"/api/v0/workflows/{workflow_id}/versions/"
        f"{detail['version']['workflow_version_id']}/graph"
    )
    assert response.status_code == 200
    graph = response.json()["graph"]
    assert [stage["execution_mode"] for stage in graph["stages"]] == [
        "sequential",
        "parallel",
        "sequential",
    ]
    joined = next(node for node in graph["nodes"] if node["agent_id"] == agent_ids[3])
    assert joined["dependencies"]["join_policy"] == "all"


def _assert_invalid_parallel_edit_is_not_published(
    client, workflow_id, agent_ids, current_version
):
    """Reject an incomplete parallel group and preserve the published version."""
    invalid = client.put(
        f"/api/v0/workflows/{workflow_id}/steps",
        json={
            "agent_ids": agent_ids,
            "parallel_groups": [{"agent_ids": agent_ids[:2]}],
        },
    )
    assert invalid.status_code == 422
    saved = client.get(f"/api/v0/workflows/{workflow_id}").json()["version"]
    assert saved["workflow_version_id"] == current_version["workflow_version_id"]


def test_api_requires_workflow_management_permission(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    now = datetime.now(UTC)
    runtime.principals.create(
        Principal(
            id="workflow-runner",
            org_id="local-org",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            status=PrincipalStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
    )
    runtime.roles.create(
        Role(
            id="workflow-runner-role",
            org_id="local-org",
            name="Workflow runner",
            permissions=[Permission.WORKFLOWS_RUN],
        )
    )
    runtime.role_bindings.create(
        RoleBinding(
            id="workflow-runner-binding",
            org_id="local-org",
            workspace_id="local-workspace",
            principal_id="workflow-runner",
            role_id="workflow-runner-role",
            created_at=now,
        )
    )
    monkeypatch.setenv(
        "WEAVES_API_TOKENS",
        json.dumps(
            [{"token": "workflow-runner-token", "principal_id": "workflow-runner"}]
        ),
    )
    monkeypatch.delenv("WEAVES_API_TOKEN", raising=False)
    client = TestClient(create_app(runtime))

    with client:
        response = client.post(
            "/api/v0/workflows",
            json={
                "name": "Forbidden workflow",
                "agent_id": "local-assistant",
            },
            headers={"Authorization": "Bearer workflow-runner-token"},
        )

    assert response.status_code == 403
