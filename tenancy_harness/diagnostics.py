"""Capture known Phase 1 defects explicitly, separate from isolation controls."""

import socket

import httpx

from tenancy_harness.client import API, PROVIDER, call, mailbox
from tenancy_harness.resources import tenant


def seed_diagnostics(fixtures):
    """Record baseline observations without weakening or mocking product policy."""
    a, b = fixtures["tenants"].values()
    descriptors = call("GET", "/plugin-descriptors", b["admin"])
    leaked = any(x["plugin_id"] == a["installation"]["plugin_id"] for x in descriptors)
    call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "openai_compatible",
            "display_name": "egress diagnostic",
            "api_key": "fixture-egress-only",
            "base_url": PROVIDER + "/egress-probe/v1",
        },
    )
    paths = httpx.get(PROVIDER + "/egress", timeout=10).json()["paths"]
    email = a["shared_person"]["email"]
    before = mailbox(email)["count"]
    result = call("POST", "/auth/email-code", body={"email": email})
    ambiguous = mailbox(email)["count"] == before and bool(result["challenge_id"])
    return {
        "T0-D01": leaked,
        "T0-D02": "/egress-probe/v1/models" in paths,
        "T0-D05": ambiguous,
        "private_provider_ip": socket.gethostbyname("provider"),
        "lifecycle": lifecycle_fixture(),
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
    denied = httpx.get(API + "/agents", headers=admin["headers"], timeout=10)
    # Organization suspension has no public API yet. This is fixture setup only.
    from apps.runtime_wiring import open_process_runtime
    from contracts.v1 import OrganizationStatus

    runtime = open_process_runtime(process="cli")
    try:
        runtime.bootstrap()
        org = runtime.organizations.get(t["org_id"])
        runtime.organizations.put(
            org.model_copy(update={"status": OrganizationStatus.SUSPENDED})
        )
    finally:
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
        "webhook_accepted_after_suspension": webhook.status_code == 202,
        "trigger": trigger,
    }
