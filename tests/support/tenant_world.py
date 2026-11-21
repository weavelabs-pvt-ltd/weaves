"""Two organizations and three workspaces populated with referenceable records."""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from contracts.v1 import PluginInstallation, PluginInstallationStatus
from shared.clock import utc_now
from tests.support.account_session import account_headers
from tests.support.organizations import onboard

CAPABILITY_ID = "slack.messages.search"
SKILL_MARKDOWN = (
    "---\nname: {name}\ndescription: Reference scope fixture.\n---\nBe brief.\n"
)


@dataclass
class Scope:
    """One workspace's credentials and the identifiers of its records."""

    name: str
    org_id: str
    workspace_id: str
    headers: dict[str, str]
    ids: dict[str, str] = field(default_factory=dict)


@dataclass
class World:
    """Org A with workspaces A1 and A2, plus org B with workspace B1."""

    client: TestClient
    runtime: object
    a1: Scope
    a2: Scope
    b1: Scope
    peer_headers: dict[str, str]
    peer_ids: dict[str, str]


def build_world(client: TestClient, runtime: object) -> World:
    """Create the tenants and populate each workspace with comparable records."""
    a1 = _onboard(client, "A")
    b1 = _onboard(client, "B")
    a2 = _extra_workspace(client, a1)
    for scope in (a1, a2, b1):
        _populate(client, runtime, scope)
    for scope in (a1, b1):
        _publish_org_skill(client, scope)
    a2.ids.update(
        {
            k: v
            for k, v in a1.ids.items()
            if k.startswith(("org_skill", "personal_skill"))
        }
    )
    peer_headers, peer_ids = _peer_with_personal_skill(client, a1)
    return World(client, runtime, a1, a2, b1, peer_headers, peer_ids)


def _onboard(client: TestClient, label: str) -> Scope:
    response = onboard(
        client,
        name=f"Org {label}",
        owner_email=f"owner-{label.lower()}@example.test",
        owner_display_name=f"Owner {label}",
    )
    body = response
    workspace_id = body["workspace"]["id"]
    headers = {
        "Authorization": f"Bearer {body['access_token']}",
        "X-Workspace-ID": workspace_id,
    }
    return Scope(f"{label}1", body["organization"]["id"], workspace_id, headers)


def _extra_workspace(client: TestClient, base: Scope) -> Scope:
    response = client.post(
        "/api/v0/workspaces", headers=base.headers, json={"name": "Second workspace"}
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["id"]
    headers = {**base.headers, "X-Workspace-ID": workspace_id}
    return Scope("A2", base.org_id, workspace_id, headers)


def _ok(response, status: int = 201) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def _populate(client: TestClient, runtime: object, scope: Scope) -> None:
    """Create one of every referenceable record inside a single workspace."""
    h, ids = scope.headers, scope.ids
    agent = _ok(
        client.post(
            "/api/v0/agents",
            headers=h,
            json={"name": f"Agent {scope.name}", "instructions": "Reply."},
        )
    )
    ids["agent"] = agent["id"]
    ids["agent_version"] = agent["current_version_id"]
    second = _ok(
        client.post(
            "/api/v0/agents",
            headers=h,
            json={"name": f"Second {scope.name}", "instructions": "Reply."},
        )
    )
    ids["agent2"] = second["id"]
    workflow = _ok(
        client.post(
            "/api/v0/workflows",
            headers=h,
            json={"name": f"Flow {scope.name}", "agent_id": ids["agent"]},
        )
    )
    ids["workflow"] = workflow["definition"]["workflow_id"]
    ids["workflow_version"] = workflow["version"]["workflow_version_id"]
    ids["task"] = _ok(
        client.post(
            "/api/v0/tasks",
            headers=h,
            json={
                "agent_id": ids["agent"],
                "title": "Task",
                "initial_message": "Hello",
            },
        ),
        202,
    )["task_id"]
    ids["policy"] = _ok(
        client.post(
            "/api/v0/approval-policies",
            headers=h,
            json={"name": f"Policy {scope.name}"},
        )
    )["approval_policy_id"]
    _automation(client, scope)
    _canvas_and_suite(client, scope)
    ids["installation"] = _install(runtime, scope)
    if scope.name != "A1":
        ids["capability"] = _install_github(runtime, scope)
    _model_and_identity_ids(client, scope)


def _canvas_and_suite(client: TestClient, scope: Scope) -> None:
    """Publish a canvas workflow, start one run and register an evaluation suite."""
    h, ids = scope.headers, scope.ids
    canvas = _ok(
        client.post(
            "/api/v0/workflow-studio/publish",
            headers=h,
            json={
                "name": f"Canvas {scope.name}",
                "plan": _plan(ids["agent"]),
                "description": "",
            },
        ),
        200,
    )
    ids["canvas_workflow"] = canvas["definition"]["workflow_id"]
    ids["run"] = _ok(
        client.post(
            f"/api/v0/workflows/{ids['canvas_workflow']}/runs",
            headers=h,
            json={"instructions": "go"},
        ),
        202,
    )["run_id"]
    ids["suite"] = f"suite-{scope.name.lower()}"
    _ok(
        client.post(
            "/api/v0/evaluation-suites",
            headers=h,
            json={"definition": _suite(ids["suite"], ids["agent"])},
        )
    )


def _suite(suite_id: str, agent_id: str) -> dict:
    return {
        "suite_id": suite_id,
        "agent_id": agent_id,
        "cases": [{"case_id": "c1", "task": "Say hi"}],
    }


def _install(runtime: object, scope: Scope) -> str:
    installation_id = f"install-{scope.name.lower()}"
    runtime.installations.create(  # type: ignore[attr-defined]
        PluginInstallation(
            plugin_installation_id=installation_id,
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            plugin_id="slack",
            display_name=f"Slack {scope.name}",
            configuration={"channels": ["testing"]},
            auth_ref="fixture-only",
            enabled_capability_ids=(CAPABILITY_ID,),
            status=PluginInstallationStatus.ACTIVE,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    return installation_id


def _model_and_identity_ids(client: TestClient, scope: Scope) -> None:
    h, ids = scope.headers, scope.ids
    ids["profile"] = client.get("/api/v0/model-profiles", headers=h).json()[0]["id"]
    ids["provider"] = client.get("/api/v0/model-providers", headers=h).json()[0]["id"]
    roles = client.get("/api/v0/roles", headers=h).json()
    ids["role"] = next(r["id"] for r in roles if r["system_role_key"] == "user")
    bindings = client.get("/api/v0/role-bindings", headers=h).json()
    ids["binding"] = bindings[0]["id"]
    ids["principal"] = bindings[0]["principal_id"]
    users = client.get("/api/v0/users", headers=h).json()
    ids["user"] = users[0]["id"]
    ids["workspace"] = scope.workspace_id
    ids["org"] = scope.org_id


def _skill_files(name: str) -> dict[str, str]:
    return {"SKILL.md": SKILL_MARKDOWN.format(name=name)}


def _publish_org_skill(client: TestClient, scope: Scope) -> None:
    created = _ok(
        client.post(
            "/api/v0/skills/personal",
            headers=scope.headers,
            json={"files": _skill_files(f"org-skill-{scope.name.lower()}")},
        )
    )
    version = created["version"]
    published = _ok(
        client.post(
            f"/api/v0/skills/{created['skill']['skill_id']}/publish",
            headers=scope.headers,
            json={
                "skill_version_id": version["skill_version_id"],
                "content_digest": version["content_digest"],
                "self_review_confirmed": True,
            },
        )
    )
    scope.ids["org_skill"] = published["skill"]["skill_id"]
    scope.ids["org_skill_version"] = published["version"]["skill_version_id"]
    scope.ids["personal_skill"] = created["skill"]["skill_id"]
    scope.ids["personal_skill_version"] = version["skill_version_id"]


def _peer_with_personal_skill(
    client: TestClient, a1: Scope
) -> tuple[dict, dict[str, str]]:
    """A second member of org A owns a private skill the owner may not use."""
    roles = client.get("/api/v0/roles", headers=a1.headers).json()
    builder = next(r["id"] for r in roles if r["system_role_key"] == "builder")
    user = _ok(
        client.post(
            "/api/v0/users",
            headers=a1.headers,
            json={
                "email": "peer@example.test",
                "display_name": "Peer",
                "role_id": builder,
            },
        )
    )
    headers = account_headers(
        client, user["user"]["email"], user["user"]["org_id"], a1.workspace_id
    )
    created = _ok(
        client.post(
            "/api/v0/skills/personal",
            headers=headers,
            json={"files": _skill_files("peer-private-skill")},
        )
    )
    version_id = created["version"]["skill_version_id"]
    return headers, {
        "personal_skill": created["skill"]["skill_id"],
        "personal_skill_version": version_id,
        "org_skill_version": version_id,
    }


def _automation(client: TestClient, scope: Scope) -> None:
    """Triggers and schedules need durable storage; record them when available."""
    h, ids = scope.headers, scope.ids
    path = f"/api/v0/workflows/{ids['workflow']}"
    trigger = client.post(
        f"{path}/triggers",
        headers=h,
        json={"name": "Hook", "task_instructions": "Run"},
    )
    if trigger.status_code == 201:
        ids["trigger"] = trigger.json()["trigger"]["trigger_id"]
    schedule = client.post(
        f"{path}/schedules",
        headers=h,
        json={"name": "Nightly", "task_instructions": "Run", "interval_seconds": 3600},
    )
    if schedule.status_code == 201:
        ids["schedule"] = schedule.json()["schedule_id"]


def _plan(agent_id: str) -> dict:
    return {"steps": [{"id": "s1", "type": "agent", "a": [agent_id], "t": "Do it"}]}


def _install_github(runtime: object, scope: Scope) -> str:
    """Enable a capability that the attacking workspace does not have."""
    runtime.installations.create(  # type: ignore[attr-defined]
        PluginInstallation(
            plugin_installation_id=f"github-{scope.name.lower()}",
            org_id=scope.org_id,
            workspace_id=scope.workspace_id,
            plugin_id="github",
            display_name=f"GitHub {scope.name}",
            configuration={},
            auth_ref="fixture-only",
            enabled_capability_ids=("github.issues.search",),
            status=PluginInstallationStatus.ACTIVE,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    return "github.issues.search"
