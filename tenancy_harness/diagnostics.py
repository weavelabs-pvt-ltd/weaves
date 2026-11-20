"""Capture known Phase 1 defects explicitly, separate from isolation controls."""

import httpx

from tenancy_harness.client import API, call, mailbox
from tenancy_harness.resources import tenant


def seed_diagnostics(fixtures):
    """Record baseline observations without weakening or mocking product policy."""
    return {
        "lifecycle": lifecycle_fixture(),
        "sign_in": sign_in_fixture(fixtures),
        "account": account_fixture(fixtures),
    }


def account_fixture(fixtures):
    """Sign in as an account with real email proof and read its memberships."""
    a, b = fixtures["tenants"]["a"], fixtures["tenants"]["b"]
    email = a["shared_person"]["email"]
    source = {"X-Forwarded-For": "198.20.0.77"}
    before = mailbox(email)["count"]
    started = httpx.post(
        API + "/account/email-code", headers=source, json={"email": email}, timeout=30
    )
    proof = mailbox(email)
    code_sent = started.status_code == 202 and proof["count"] == before + 1
    verified = httpx.post(
        API + "/account/email-verify",
        headers=source,
        json={"challenge_id": started.json()["challenge_id"], "code": proof["code"]},
        timeout=30,
    )
    token = verified.json().get("access_token", "")
    bearer = {"Authorization": "Bearer " + token}
    me = httpx.get(API + "/account/me", headers=bearer, timeout=30)
    organizations = {
        item["organization_id"]
        for item in (me.json().get("memberships", []) if me.status_code == 200 else [])
    }
    stranger = httpx.post(
        API + "/account/email-code",
        headers=source,
        json={"email": "nobody.at.all@tenancy.test"},
        timeout=30,
    )
    return {
        "code_sent": code_sent,
        "signed_in": verified.status_code == 200 and token.startswith("wvs_acct_"),
        "one_account_two_organizations": organizations == {a["org_id"], b["org_id"]},
        "account_token_denied_on_tenant_routes": all(
            httpx.get(API + path, headers=bearer, timeout=30).status_code == 401
            for path in ("/agents", "/workspaces", "/model-providers")
        ),
        "tenant_token_denied_on_account_routes": httpx.get(
            API + "/account/me", headers=a["admin"]["headers"], timeout=30
        ).status_code
        == 401,
        "unknown_email_indistinguishable": stranger.status_code == started.status_code
        and set(stranger.json()) == set(started.json()),
    }


def sign_in_fixture(fixtures):
    """Exhaust one tenant's email allowance, then prove the other is unaffected."""
    a, b = fixtures["tenants"]["a"], fixtures["tenants"]["b"]
    email = a["shared_person"]["email"]
    before = mailbox(email)["count"]
    unscoped = call("POST", "/auth/email-code", body={"email": email})
    ambiguous = mailbox(email)["count"] == before and bool(unscoped["challenge_id"])

    def request_code(org_id, source):
        return httpx.post(
            API + "/auth/email-code",
            headers={"X-Forwarded-For": f"198.19.{source // 250}.{source % 250 + 1}"},
            json={"email": email, "organization_id": org_id},
            timeout=30,
        ).status_code

    statuses = [request_code(a["org_id"], index) for index in range(1, 13)]
    return {
        "shared_person_in_both": all(
            bool(t["shared_person"]["headers"]) for t in (a, b)
        ),
        "unscoped_ambiguous_issues_no_code": ambiguous,
        "tenant_a_throttled": 429 in statuses,
        "tenant_b_unaffected": request_code(b["org_id"], 200) == 202,
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
