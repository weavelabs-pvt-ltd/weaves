"""Reference-bearing requests for models, identity, skills, evaluations."""

from __future__ import annotations

from tests.contracts.reference_scope_cases import ORG, SKILL, WS, case


@case("model_configuration.provider_id", ORG)
def _(w, ref):
    body = {"mode": "organization_managed", "provider_id": ref("provider")}
    return "PUT", "/api/v0/model-configuration", body


@case("model_configuration.model_profile_id", ORG)
def _(w, ref):
    body = {"mode": "organization_managed", "model_profile_id": ref("profile")}
    return "PUT", "/api/v0/model-configuration", body


@case("model_profile.patch.path_profile_id", ORG)
def _(w, ref):
    path = f"/api/v0/model-profiles/{ref('profile')}"
    return "PATCH", path, {"status": "disabled"}


@case("model_provider.credential.path_provider_id", ORG)
def _(w, ref):
    path = f"/api/v0/model-providers/{ref('provider')}/credential"
    return "PUT", path, {"api_key": "sk-not-real"}


@case("model_provider.refresh.path_provider_id", ORG)
def _(w, ref):
    return "POST", f"/api/v0/model-providers/{ref('provider')}/refresh-models", None


@case("installation.patch.path_installation_id", WS)
def _(w, ref):
    path = f"/api/v0/plugin-installations/{ref('installation')}"
    return "PATCH", path, {"status": "disabled"}


@case("installation.delete.path_installation_id", WS)
def _(w, ref):
    return "DELETE", f"/api/v0/plugin-installations/{ref('installation')}", None


@case("installation.healthcheck.path_installation_id", WS)
def _(w, ref):
    path = f"/api/v0/plugin-installations/{ref('installation')}/healthcheck"
    return "POST", path, None


@case("role_binding.create.principal_id", ORG)
def _(w, ref):
    body = {"principal_id": ref("principal"), "role_id": w.a1.ids["role"]}
    return "POST", "/api/v0/role-bindings", body


@case("role_binding.create.role_id", ORG)
def _(w, ref):
    body = {"principal_id": w.a1.ids["principal"], "role_id": ref("role")}
    return "POST", "/api/v0/role-bindings", body


@case("role_binding.create.workspace_id", ORG)
def _(w, ref):
    body = {
        "principal_id": w.a1.ids["principal"],
        "role_id": w.a1.ids["role"],
        "workspace_id": ref("workspace"),
    }
    return "POST", "/api/v0/role-bindings", body


@case("role_binding.put.workspace_id", ORG)
def _(w, ref):
    body = {
        "principal_id": w.a1.ids["principal"],
        "role_id": w.a1.ids["role"],
        "workspace_id": ref("workspace"),
    }
    return "PUT", "/api/v0/role-bindings", body


@case("role_binding.delete.path_binding_id", ORG)
def _(w, ref):
    return "DELETE", f"/api/v0/role-bindings/{ref('binding')}", None


@case("user.create.role_id", ORG)
def _(w, ref):
    body = {"email": "new@example.test", "display_name": "N", "role_id": ref("role")}
    return "POST", "/api/v0/users", body


@case("user.suspend.path_user_id", ORG)
def _(w, ref):
    return "POST", f"/api/v0/users/{ref('user')}/suspend", None


@case("workspace.patch.path_workspace_id", ORG)
def _(w, ref):
    return "PATCH", f"/api/v0/workspaces/{ref('workspace')}", {"name": "Renamed"}


@case("skill.publish.skill_version_id", SKILL)
def _(w, ref):
    path = f"/api/v0/skills/{w.a1.ids['personal_skill']}/publish"
    body = {
        "skill_version_id": ref("personal_skill_version"),
        "content_digest": "0" * 64,
        "self_review_confirmed": True,
    }
    return "POST", path, body


@case("skill.publish.path_skill_id", SKILL)
def _(w, ref):
    path = f"/api/v0/skills/{ref('personal_skill')}/publish"
    body = {
        "skill_version_id": w.a1.ids["personal_skill_version"],
        "content_digest": "0" * 64,
        "self_review_confirmed": True,
    }
    return "POST", path, body


@case("skill.restore.source_version_id", SKILL)
def _(w, ref):
    body = {
        "source_version_id": ref("org_skill_version"),
        "expected_current_version_id": w.a1.ids["org_skill_version"],
        "self_review_confirmed": True,
    }
    return "POST", f"/api/v0/skills/{w.a1.ids['org_skill']}/restore", body


@case("skill.revoke.path_version_id", SKILL)
def _(w, ref):
    path = f"/api/v0/skills/versions/{ref('org_skill_version')}/revoke"
    return "POST", path, {"reason": "probe"}


@case("skill.read_version.path_version_id", SKILL)
def _(w, ref):
    return "GET", f"/api/v0/skills/versions/{ref('personal_skill_version')}", None


@case("skill.versions.path_skill_id", SKILL)
def _(w, ref):
    return "GET", f"/api/v0/skills/{ref('personal_skill')}/versions", None


@case("evaluation.suite.create.agent_id", WS)
def _(w, ref):
    suite = {
        "suite_id": "probe-suite",
        "agent_id": ref("agent"),
        "cases": [{"case_id": "c1", "task": "Say hi"}],
    }
    return "POST", "/api/v0/evaluation-suites", {"definition": suite}


@case("evaluation.suite.update.agent_id", WS)
def _(w, ref):
    suite = {
        "suite_id": w.a1.ids["suite"],
        "agent_id": ref("agent"),
        "cases": [{"case_id": "c1", "task": "Say hi"}],
    }
    path = f"/api/v0/evaluation-suites/{w.a1.ids['suite']}"
    return "PUT", path, {"definition": suite}


@case("evaluation.suite.read.path_suite_id", WS)
def _(w, ref):
    return "GET", f"/api/v0/evaluation-suites/{ref('suite')}", None


@case("evaluation.report.result_run_id", WS)
def _(w, ref):
    suite = {
        "suite_id": w.a1.ids["suite"],
        "agent_id": w.a1.ids["agent"],
        "cases": [{"case_id": "c1", "task": "Say hi"}],
    }
    check = {"name": "n", "passed": True, "detail": "d"}
    result = {"case_id": "c1", "passed": True, "checks": [check]}
    report = {
        "suite_id": w.a1.ids["suite"],
        "evaluation_id": "probe-report",
        "passed_cases": 1,
        "total_cases": 1,
        "pass_rate": 1.0,
        "results": [{**result, "run_id": ref("task")}],
    }
    return "POST", "/api/v0/evaluation-reports", {"definition": suite, "report": report}


@case("evaluation.suite.run.path_suite_id", WS)
def _(w, ref):
    return "POST", f"/api/v0/evaluation-suites/{ref('suite')}/runs", None
