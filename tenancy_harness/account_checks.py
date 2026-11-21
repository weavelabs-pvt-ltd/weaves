"""Real-stack account controls: sign-in, organization selection, email linking."""

import httpx

from tenancy_harness.client import API, mailbox

SOURCE = {"X-Forwarded-For": "198.20.0.77"}
SECOND_EMAIL = "shared.person.second@tenancy.test"


def _post(path, headers, body):
    return httpx.post(API + path, headers=headers, json=body, timeout=30)


def sign_in_checks(a, b):
    """Prove one person signs in with real email proof; return (bearer, checks)."""
    email = a["shared_person"]["email"]
    before = mailbox(email)["count"]
    started = _post("/account/email-code", SOURCE, {"email": email})
    proof = mailbox(email)
    verified = _post(
        "/account/email-verify",
        SOURCE,
        {"challenge_id": started.json()["challenge_id"], "code": proof["code"]},
    )
    token = verified.json().get("access_token", "")
    bearer = {"Authorization": "Bearer " + token}
    me = httpx.get(API + "/account/me", headers=bearer, timeout=30)
    organizations = {
        item["organization_id"]
        for item in (me.json().get("memberships", []) if me.status_code == 200 else [])
    }
    stranger = _post(
        "/account/email-code", SOURCE, {"email": "nobody.at.all@tenancy.test"}
    )
    return bearer, {
        "code_sent": started.status_code == 202 and proof["count"] == before + 1,
        "signed_in": verified.status_code == 200 and token.startswith("wvs_acct_"),
        "one_account_two_organizations": organizations == {a["org_id"], b["org_id"]},
        "account_token_denied_on_tenant_routes": all(
            httpx.get(API + path, headers=bearer, timeout=30).status_code == 401
            for path in ("/agents", "/workspaces", "/model-providers")
        ),
        "tenant_token_denied_on_account_routes": httpx.get(
            API + "/account/me", headers=a["service"]["headers"], timeout=30
        ).status_code
        == 401,
        "unknown_email_indistinguishable": stranger.status_code == started.status_code
        and set(stranger.json()) == set(started.json()),
    }


def selection_checks(a, b, bearer, foreign_org_id):
    """Prove the organization is chosen per request and refusals look the same."""

    def names(org_id, actor_headers=None, workspace=None):
        headers = dict(actor_headers or bearer)
        headers["X-Organization-Id"] = org_id
        if workspace:
            headers["X-Workspace-ID"] = workspace
        response = httpx.get(API + "/agents", headers=headers, timeout=30)
        body = response.json() if response.status_code == 200 else None
        return response, {item["name"] for item in body or []}

    _, names_a = names(a["org_id"])
    _, names_b = names(b["org_id"])
    unknown, _ = names("no-such-organization")
    foreign, _ = names(foreign_org_id)
    ignored, names_ignored = names(b["org_id"], a["service"]["headers"])
    cross_workspace, _ = names(a["org_id"], workspace=b["workspace_id"])
    return {
        "selects_each_organization": a["agent"]["name"] in names_a
        and b["agent"]["name"] in names_b
        and a["agent"]["name"] not in names_b
        and b["agent"]["name"] not in names_a,
        "foreign_and_unknown_indistinguishable": unknown.status_code == 403
        and (foreign.status_code, foreign.json())
        == (unknown.status_code, unknown.json()),
        "tenant_token_ignores_header": ignored.status_code == 200
        and a["agent"]["name"] in names_ignored
        and b["agent"]["name"] not in names_ignored,
        "workspace_checked_inside_organization": cross_workspace.status_code == 403,
    }


def _prove(path, email, headers):
    """Request a proof code for an address and redeem it with the real mailbox."""
    before = mailbox(email)["count"]
    started = _post(path + "email-code", headers, {"email": email})
    assert mailbox(email)["count"] == before + 1
    return _post(
        path + "email-verify",
        headers,
        {
            "challenge_id": started.json()["challenge_id"],
            "code": mailbox(email)["code"],
        },
    )


def linking_checks(a, b, bearer):
    """Prove a second email needs proof and another account's email is refused."""
    other_email = b["admin"]["email"]
    other = _prove("/account/", other_email, {"X-Forwarded-For": "198.20.0.78"})
    attached = _prove("/account/identities/", SECOND_EMAIL, {**SOURCE, **bearer})
    conflict = _prove("/account/identities/", other_email, {**SOURCE, **bearer})
    me_after = httpx.get(API + "/account/me", headers=bearer, timeout=30)
    other_me = httpx.get(
        API + "/account/me",
        headers={"Authorization": "Bearer " + other.json()["access_token"]},
        timeout=30,
    )
    return {
        "email_attached_after_proof": attached.status_code == 200
        and len(attached.json()["identities"]) == 2,
        "email_of_another_account_refused": conflict.status_code == 409,
        "refusal_changed_nothing": me_after.status_code == 200
        and len(me_after.json()["identities"]) == 2
        and other_me.status_code == 200
        and len(other_me.json()["identities"]) == 1,
    }


def creation_checks(a, b, bearer):
    """Prove an account creates an organization once, idempotently and privately."""
    headers = {**SOURCE, **bearer, "Idempotency-Key": "harness-create-1"}
    body = {"name": "Harness Created Organization"}
    first = _post("/account/organizations", headers, body)
    replay = _post("/account/organizations", headers, body)
    changed = _post("/account/organizations", headers, {"name": "A Different Name"})
    created = (
        first.json().get("organization", {}).get("id", "")
        if first.status_code == 201
        else ""
    )
    selected = httpx.get(
        API + "/agents",
        headers={**bearer, "X-Organization-Id": created},
        timeout=30,
    )
    stranger = _prove(
        "/account/", b["user"]["email"], {"X-Forwarded-For": "198.20.0.79"}
    )
    refused = httpx.get(
        API + "/agents",
        headers={
            "Authorization": "Bearer " + stranger.json()["access_token"],
            "X-Organization-Id": created,
        },
        timeout=30,
    )
    return {
        "organization_created": first.status_code == 201 and bool(created),
        "replay_returns_same_organization": replay.status_code == 200
        and replay.json()["organization"]["id"] == created,
        "key_reuse_with_other_body_conflicts": changed.status_code == 409,
        "creator_can_select_it": selected.status_code == 200,
        "another_account_is_refused": refused.status_code == 403,
        "no_tenant_token_issued": "access_token" not in first.json()
        and "token" not in first.text.lower(),
    }
