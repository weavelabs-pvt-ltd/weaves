"""Reference-bearing requests: every body field and nested step that names a record."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tests.support.tenant_world import World

Ref = Callable[[str], str]
Request = tuple[str, str, dict | None]
Builder = Callable[[World, Ref], Request]


@dataclass(frozen=True)
class Case:
    """One request whose reference is replaced by a foreign or missing record.

    ``sources`` names the foreign places the reference may point into: ``org``
    is another organization, ``ws`` another workspace of the same organization
    and ``peer`` another member's private skill in the same organization.
    """

    name: str
    sources: tuple[str, ...]
    build: Builder
    needs: str | None = None
    missing: tuple[tuple[str, str], ...] = ()


WS = ("org", "ws")
ORG = ("org",)
SKILL = ("org", "peer")
CASES: list[Case] = []


def case(
    name: str,
    sources: tuple[str, ...],
    needs: str | None = None,
    missing: tuple[tuple[str, str], ...] = (),
):
    def register(build: Builder) -> Builder:
        CASES.append(Case(name, sources, build, needs, missing))
        return build

    return register


def _agent_body(**extra: object) -> dict:
    return {"name": "Probe agent", "instructions": "Reply.", **extra}


@case("agent.create.model_profile_id", ORG)
def _(w, ref):
    return "POST", "/api/v0/agents", _agent_body(model_profile_id=ref("profile"))


@case("agent.create.allowed_capability_ids", WS)
def _(w, ref):
    body = _agent_body(allowed_capability_ids=[ref("capability")])
    return "POST", "/api/v0/agents", body


@case("agent.create.skill_version_ids", SKILL)
def _(w, ref):
    body = _agent_body(skill_version_ids=[ref("org_skill_version")])
    return "POST", "/api/v0/agents", body


@case("agent.template.create.model_profile_id", ORG)
def _(w, ref):
    path = "/api/v0/agent-templates/engineering-assistant/agents"
    return "POST", path, {"model_profile_id": ref("profile")}


@case(
    "agent.template.create.allowed_capability_ids",
    WS,
    missing=(("capability", "jira.issues.search"),),
)
def _(w, ref):
    path = "/api/v0/agent-templates/engineering-assistant/agents"
    return "POST", path, {"allowed_capability_ids": [ref("capability")]}


@case("agent.template.create.skill_version_ids", SKILL)
def _(w, ref):
    path = "/api/v0/agent-templates/engineering-assistant/agents"
    return "POST", path, {"skill_version_ids": [ref("org_skill_version")]}


@case("agent.patch.model_profile_id", ORG)
def _(w, ref):
    path = f"/api/v0/agents/{w.a1.ids['agent']}"
    return "PATCH", path, {"model_profile_id": ref("profile")}


@case("agent.patch.allowed_capability_ids", WS)
def _(w, ref):
    path = f"/api/v0/agents/{w.a1.ids['agent']}"
    return "PATCH", path, {"allowed_capability_ids": [ref("capability")]}


@case("agent.patch.skill_version_ids", SKILL)
def _(w, ref):
    path = f"/api/v0/agents/{w.a1.ids['agent']}"
    return "PATCH", path, {"skill_version_ids": [ref("org_skill_version")]}


@case("agent.patch.path_agent_id", WS)
def _(w, ref):
    return "PATCH", f"/api/v0/agents/{ref('agent')}", {"name": "Renamed"}


@case("agent.read.path_agent_id", WS)
def _(w, ref):
    return "GET", f"/api/v0/agents/{ref('agent')}", None


@case("agent.versions.path_agent_id", WS)
def _(w, ref):
    return "GET", f"/api/v0/agents/{ref('agent')}/versions", None


@case("agent.version.path_version_id", WS)
def _(w, ref):
    path = f"/api/v0/agents/{w.a1.ids['agent']}/versions/{ref('agent_version')}"
    return "GET", path, None


@case("agent.archive.path_agent_id", WS)
def _(w, ref):
    return "POST", f"/api/v0/agents/{ref('agent')}/archive", None


@case("task.create.agent_id", WS)
def _(w, ref):
    body = {"agent_id": ref("agent"), "title": "T", "initial_message": "Hi"}
    return "POST", "/api/v0/tasks", body


@case("task.create.skill_version_ids", SKILL)
def _(w, ref):
    body = {
        "agent_id": w.a1.ids["agent"],
        "title": "T",
        "initial_message": "Hi",
        "audience": "private",
        "skill_version_ids": [ref("personal_skill_version")],
    }
    return "POST", "/api/v0/tasks", body


@case("task.read.path_task_id", WS)
def _(w, ref):
    return "GET", f"/api/v0/tasks/{ref('task')}", None


@case("task.message.path_task_id", WS)
def _(w, ref):
    return "POST", f"/api/v0/tasks/{ref('task')}/messages", {"content": "more"}


@case("task.complete.path_task_id", WS)
def _(w, ref):
    return "POST", f"/api/v0/tasks/{ref('task')}/complete", None


@case("workflow.create.agent_id", WS)
def _(w, ref):
    return "POST", "/api/v0/workflows", {"name": "Flow X", "agent_id": ref("agent")}


@case("workflow.create.agent_ids", WS)
def _(w, ref):
    ids = [w.a1.ids["agent"], ref("agent")]
    return "POST", "/api/v0/workflows", {"name": "Flow X", "agent_ids": ids}


def _steps(w: World) -> str:
    return f"/api/v0/workflows/{w.a1.ids['workflow']}/steps"


@case("workflow.steps.agent_ids", WS)
def _(w, ref):
    return "PUT", _steps(w), {"agent_ids": [w.a1.ids["agent"], ref("agent")]}


@case("workflow.steps.step_conditions.source_agent_id", WS)
def _(w, ref):
    body = {
        "agent_ids": [w.a1.ids["agent"], w.a1.ids["agent2"]],
        "step_conditions": [_condition(ref("agent"), w.a1.ids["agent2"])],
    }
    return "PUT", _steps(w), body


@case("workflow.steps.step_conditions.target_agent_id", WS)
def _(w, ref):
    body = {
        "agent_ids": [w.a1.ids["agent"], w.a1.ids["agent2"]],
        "step_conditions": [_condition(w.a1.ids["agent"], ref("agent"))],
    }
    return "PUT", _steps(w), body


def _condition(source: str, target: str) -> dict:
    return {
        "source_agent_id": source,
        "target_agent_id": target,
        "json_pointer": "/ok",
        "operator": "exists",
    }


@case("workflow.steps.parallel_groups.agent_ids", WS)
def _(w, ref):
    body = {
        "agent_ids": [w.a1.ids["agent"], w.a1.ids["agent2"]],
        "parallel_groups": [{"agent_ids": [w.a1.ids["agent"], ref("agent")]}],
    }
    return "PUT", _steps(w), body


@case("workflow.steps.step_dependencies.target_agent_id", WS)
def _(w, ref):
    body = {
        "agent_ids": [w.a1.ids["agent"], w.a1.ids["agent2"]],
        "step_dependencies": [
            {
                "target_agent_id": ref("agent"),
                "depends_on_agent_ids": [w.a1.ids["agent"]],
            }
        ],
    }
    return "PUT", _steps(w), body


@case("workflow.steps.step_dependencies.depends_on_agent_ids", WS)
def _(w, ref):
    body = {
        "agent_ids": [w.a1.ids["agent"], w.a1.ids["agent2"]],
        "step_dependencies": [
            {
                "target_agent_id": w.a1.ids["agent2"],
                "depends_on_agent_ids": [ref("agent")],
            }
        ],
    }
    return "PUT", _steps(w), body


@case("workflow.steps.path_workflow_id", WS)
def _(w, ref):
    path = f"/api/v0/workflows/{ref('workflow')}/steps"
    return "PUT", path, {"agent_ids": [w.a1.ids["agent"]]}


@case("workflow.read.path_workflow_id", WS)
def _(w, ref):
    return "GET", f"/api/v0/workflows/{ref('workflow')}", None


@case("workflow.version.path_version_id", WS)
def _(w, ref):
    path = (
        f"/api/v0/workflows/{w.a1.ids['workflow']}/versions/{ref('workflow_version')}"
    )
    return "GET", path, None


@case("workflow.version_graph.path_version_id", WS)
def _(w, ref):
    base = f"/api/v0/workflows/{w.a1.ids['workflow']}"
    return "GET", f"{base}/versions/{ref('workflow_version')}/graph", None


@case("workflow.schedule.create.path_workflow_id", WS, needs="schedule")
def _(w, ref):
    body = {"name": "S", "task_instructions": "Run", "interval_seconds": 3600}
    return "POST", f"/api/v0/workflows/{ref('workflow')}/schedules", body


@case("workflow.schedule.patch.path_schedule_id", WS, needs="schedule")
def _(w, ref):
    return "PATCH", f"/api/v0/workflow-schedules/{ref('schedule')}", {"name": "N"}


@case("workflow.trigger.create.path_workflow_id", WS, needs="trigger")
def _(w, ref):
    body = {"name": "T", "task_instructions": "Run"}
    return "POST", f"/api/v0/workflows/{ref('workflow')}/triggers", body


@case("workflow.trigger.patch.path_trigger_id", WS, needs="trigger")
def _(w, ref):
    return "PATCH", f"/api/v0/workflow-triggers/{ref('trigger')}", {"name": "N"}


@case("studio.publish.plan_step_agent", WS)
def _(w, ref):
    plan = {"steps": [{"id": "s1", "type": "agent", "a": [ref("agent")], "t": "Go"}]}
    return "POST", "/api/v0/workflow-studio/publish", {"name": "Canvas X", "plan": plan}


@case("studio.publish.plan_parallel_agents", WS)
def _(w, ref):
    agents = [w.a1.ids["agent"], ref("agent")]
    plan = {"steps": [{"id": "s1", "type": "par", "a": agents, "t": "Go"}]}
    return "POST", "/api/v0/workflow-studio/publish", {"name": "Canvas X", "plan": plan}


@case("studio.publish.workflow_id", WS)
def _(w, ref):
    plan = {
        "steps": [{"id": "s", "type": "agent", "a": [w.a1.ids["agent"]], "t": "Go"}]
    }
    body = {"name": "Canvas X", "plan": plan, "workflow_id": ref("workflow")}
    return "POST", "/api/v0/workflow-studio/publish", body


@case("studio.run.retry_of", WS)
def _(w, ref):
    path = f"/api/v0/workflows/{w.a1.ids['canvas_workflow']}/runs"
    return "POST", path, {"instructions": "go", "retry_of": ref("run")}


@case("studio.run.path_run_id", WS)
def _(w, ref):
    base = f"/api/v0/workflows/{w.a1.ids['canvas_workflow']}"
    return "POST", f"{base}/runs/{ref('run')}/stop", None


@case("studio.run.path_workflow_id", WS)
def _(w, ref):
    path = f"/api/v0/workflows/{ref('canvas_workflow')}/runs"
    return "POST", path, {"instructions": "go"}
