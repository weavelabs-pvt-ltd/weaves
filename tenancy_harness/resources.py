"""Create sentinel resources through real scoped product HTTP routes."""

from tenancy_harness.client import (
    PROVIDER,
    account_session,
    actor,
    call,
    service_actor,
    source_for,
)


def member(tenant, role, email=None):
    """Provision a member by admin route, then sign in through Google."""
    email = email or f"{tenant['label']}.{role}@tenancy.test"
    roles = call("GET", "/roles", tenant["admin"])
    role_id = next(item["id"] for item in roles if item["system_role_key"] == role)
    result = call(
        "POST",
        "/users",
        tenant["admin"],
        {
            "email": email,
            "display_name": tenant["marker"] + " " + role,
            "role_id": role_id,
        },
    )
    # Signing in first links the membership; only then is the organization usable.
    session = account_session(email)
    person = actor(
        session,
        tenant["org_id"],
        tenant["workspace_id"],
        result["principal"]["id"],
        email,
    )
    call("GET", "/agents", person)
    return person


def tenant(label, timezone=None):
    """Create a real org through an owner account, with no local defaults seeded."""
    marker = "TENANCY_" + label.upper() + "_SENTINEL"
    email = f"{label}.admin@tenancy.test"
    session = account_session(email)
    created = call(
        "POST",
        "/account/browser/organizations",
        {
            "headers": {
                **session,
                "Idempotency-Key": "tenancy-" + label,
                **source_for(email),
            }
        },
        {"name": marker, "display_name": marker}
        | ({"timezone": timezone} if timezone else {}),
    )
    org_id, workspace_id = created["organization"]["id"], created["workspace"]["id"]
    admin = actor(
        session, org_id, workspace_id, created["membership"]["principal_id"], email
    )
    second = call("POST", "/workspaces", admin, {"name": marker + " second"})
    return {
        "label": label,
        "marker": marker,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "second_workspace_id": second["id"],
        "admin": admin,
    }


def definitions(t):
    """Create model, connector, agent and workflow sentinel definitions."""
    admin, marker = t["admin"], t["marker"]
    provider = call(
        "POST",
        "/model-providers",
        admin,
        {
            "provider_type": "openai_compatible",
            "display_name": marker,
            "api_key": "fixture-model-" + t["label"],
            "base_url": "https://models.tenancy.test/v1",
            "initial_model": "tenancy-fixture-model",
        },
    )
    installation = call(
        "POST",
        "/plugin-installations",
        admin,
        {
            "plugin_id": "mcp",
            "display_name": marker + " connector",
            "endpoint_url": PROVIDER + "/mcp",
            "bearer_token": marker,
            "read_only_tool_names": ["search_docs"],
            "catalog_connector_id": "custom-mcp",
        },
    )
    agent = call(
        "POST",
        "/agents",
        admin,
        {
            "name": "tenancy-" + t["label"],
            "instructions": marker,
            "description": marker,
            "allowed_capability_ids": [],
        },
    )
    workflow = call(
        "POST", "/workflows", admin, {"name": marker, "agent_id": agent["id"]}
    )
    t.update(
        {
            "provider_id": provider["id"],
            "installation": installation,
            "agent": agent,
            "workflow": workflow,
        }
    )


def resources(t):
    """Create definitions, owner-private content, roles and queued conversations."""
    definitions(t)
    admin, marker, agent = t["admin"], t["marker"], t["agent"]
    skill = call(
        "POST",
        "/skills/personal",
        t["user"],
        {
            "files": {
                "SKILL.md": (
                    f"---\nname: tenancy-{t['label']}\n"
                    f"description: {marker}\n---\n{marker}"
                )
            }
        },
    )
    private = call(
        "POST",
        "/tasks",
        t["user"],
        {
            "agent_id": agent["id"],
            "title": marker + " private",
            "initial_message": marker + " private",
            "audience": "private",
        },
    )
    task = call(
        "POST",
        "/tasks",
        admin,
        {
            "agent_id": agent["id"],
            "title": marker + " task",
            "initial_message": marker + " task",
        },
    )
    service = call(
        "POST",
        "/service-accounts",
        admin,
        {"name": marker + " robot", "permissions": ["agents.view", "tasks.view"]},
    )
    policy = call(
        "POST",
        "/approval-policies",
        admin,
        {
            "name": marker,
            "required_risk_levels": ["high"],
            "allow_self_approval": False,
        },
    )
    t.update(
        {
            "skill": skill,
            "private_task": private,
            "task": task,
            "policy": policy,
            "service": service_actor(
                service["token"], t["workspace_id"], service["principal"]["id"]
            ),
        }
    )
    automations(t)


def automations(t):
    """Seed evaluation, automation and publication sentinels without due schedules."""
    admin, marker = t["admin"], t["marker"]
    workflow = "/workflows/" + t["workflow"]["definition"]["workflow_id"]
    t["schedule"] = call(
        "POST",
        workflow + "/schedules",
        admin,
        {"name": marker, "task_instructions": marker, "interval_seconds": 86400},
    )
    t["trigger"] = call(
        "POST",
        workflow + "/triggers",
        admin,
        {"name": marker, "task_instructions": marker},
    )
    t["evaluation"] = call(
        "POST",
        "/evaluation-suites",
        admin,
        {
            "definition": {
                "suite_id": "tenancy-" + t["label"],
                "agent_id": t["agent"]["id"],
                "cases": [
                    {"case_id": "sentinel", "task": marker, "must_contain": [marker]}
                ],
            }
        },
    )
    version = t["skill"]["version"]
    t["org_skill"] = call(
        "POST",
        "/skills/" + t["skill"]["skill"]["skill_id"] + "/publish",
        t["user"],
        {
            "skill_version_id": version["skill_version_id"],
            "content_digest": version["content_digest"],
            "self_review_confirmed": True,
        },
    )


def secondary(t):
    """Seed a second workspace to test same-organization workspace isolation."""
    scope = {
        **t["admin"],
        "headers": {
            **t["admin"]["headers"],
            "X-Workspace-ID": t["second_workspace_id"],
        },
    }
    t["second_agent"] = call(
        "POST",
        "/agents",
        scope,
        {
            "name": "tenancy-second-" + t["label"],
            "instructions": t["marker"] + " second workspace",
        },
    )
