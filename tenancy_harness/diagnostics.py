"""Capture known Phase 1 defects explicitly, separate from isolation controls."""

import httpx

from tenancy_harness.account_checks import (
    creation_checks,
    identity_checks,
    onboarding_checks,
    selection_checks,
    sign_in_checks,
)
from tenancy_harness.client import API, call, request
from tenancy_harness.resources import tenant


def seed_diagnostics(fixtures):
    """Record baseline observations without weakening or mocking product policy."""
    lifecycle = lifecycle_fixture()
    return {
        "lifecycle": lifecycle,
        "account": account_fixture(fixtures, lifecycle["org_id"]),
    }


def account_fixture(fixtures, foreign_org_id):
    """Run the Google sign-in, selection, identity and creation controls."""
    a, b = fixtures["tenants"]["a"], fixtures["tenants"]["b"]
    session, signed_in = sign_in_checks(a, b)
    return {
        **signed_in,
        **selection_checks(a, b, session, foreign_org_id),
        **identity_checks(a),
        **creation_checks(a, b, session),
        **onboarding_checks(),
    }


def lifecycle_fixture():
    """Queue work and archive its workspace before any worker process starts."""
    t = tenant("lifecycle")
    admin = t["admin"]
    agent = call(
        "POST",
        "/agents",
        admin,
        {"name": "tenancy-lifecycle", "instructions": "Lifecycle fixture"},
    )
    workflow = call(
        "POST",
        "/workflows",
        admin,
        {"name": "Lifecycle diagnostic", "agent_id": agent["id"]},
    )
    task = call(
        "POST",
        "/tasks",
        admin,
        {
            "agent_id": agent["id"],
            "title": "Lifecycle diagnostic",
            "initial_message": "Lifecycle diagnostic",
        },
    )
    trigger = call(
        "POST",
        "/workflows/" + workflow["definition"]["workflow_id"] + "/triggers",
        admin,
        {"name": "Lifecycle diagnostic", "task_instructions": "Run"},
    )
    call("PATCH", "/workspaces/" + t["workspace_id"], admin, {"status": "archived"})
    denied = request("GET", "/agents", admin["headers"])
    # Organization suspension has no public API yet. This is fixture setup only.
    from apps.runtime_wiring import open_process_runtime
    from contracts.v1 import OrganizationStatus
    from shared.request_context import reset_request_principal, set_request_identity

    runtime = open_process_runtime(process="cli")
    scope = set_request_identity("tenancy-fixture", t["org_id"], t["workspace_id"])
    try:
        runtime.bootstrap()
        org = runtime.organizations.get(t["org_id"])
        runtime.organizations.put(
            org.model_copy(update={"status": OrganizationStatus.SUSPENDED})
        )
    finally:
        reset_request_principal(scope)
        runtime.close()
    webhook = httpx.post(
        API + trigger["webhook_url"].removeprefix("/api/v0"),
        headers={
            "X-Workflow-Trigger-Secret": trigger["secret"],
            "X-Event-ID": "phase-zero-suspended",
        },
        json={"fixture": "lifecycle"},
        timeout=30,
    )
    return {
        "task_id": task["task_id"],
        "org_id": t["org_id"],
        "workspace_id": t["workspace_id"],
        "api_denied": denied.status_code == 403,
        "webhook_status": webhook.status_code,
        "trigger": trigger,
    }
