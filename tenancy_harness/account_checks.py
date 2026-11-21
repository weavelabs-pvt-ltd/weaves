"""Real-stack account controls: Google sign-in, organization selection, creation."""

import httpx

from tenancy_harness.client import (
    API,
    account_session,
    google_sign_in,
    request,
    source_for,
)
from tenancy_harness.operator import operator_bearer

OUTSIDER = "outsider@outside.example"
UNVERIFIED = "unverified.person@tenancy.test"
DENIER = "denied.person@tenancy.test"
REMOVAL = "identity.removal@tenancy.test"
ERROR = "/#account-signin?error="


def refused_sign_in(email, code, **flags):
    """True when sign-in ends in the fixed error redirect with no session cookie."""
    location, token = google_sign_in(email, **flags)
    return location == ERROR + code and not token


def sign_in_checks(a, b):
    """Prove one person signs in through Google; return (session headers, checks)."""
    email = a["shared_person"]["email"]
    session = account_session(email)
    me = request("GET", "/account/browser/me", session)
    organizations = {
        item["organization_id"]
        for item in (me.json().get("memberships", []) if me.status_code == 200 else [])
    }
    service = a["service"]["headers"]
    without_csrf = {k: v for k, v in session.items() if k != "X-Weaves-CSRF"}
    return session, {
        "signed_in": me.status_code == 200 and bool(me.json().get("csrf_token")),
        "one_account_two_organizations": organizations == {a["org_id"], b["org_id"]},
        "session_without_organization_denied_on_tenant_routes": all(
            request("GET", path, session).status_code == 401
            for path in ("/agents", "/workspaces", "/model-providers")
        ),
        "tenant_credential_denied_on_account_routes": all(
            request("GET", path, service).status_code == 401
            for path in ("/account/browser/me", "/account/me")
        ),
        "csrf_enforced": request(
            "POST",
            "/account/browser/organizations",
            {**without_csrf, "Idempotency-Key": "harness-no-csrf"},
            {"name": "No CSRF"},
        ).status_code
        == 403,
        **refusal_checks(),
    }


def refusal_checks():
    """Prove disallowed, unverified and denied sign-ins fail and create nothing."""
    return {
        "email_outside_allowlist_not_allowed": refused_sign_in(OUTSIDER, "not_allowed"),
        "email_outside_allowlist_creates_nothing": operator_bearer(OUTSIDER) is None,
        "unverified_google_email_refused": refused_sign_in(
            UNVERIFIED, "access_denied", verified=False
        ),
        "unverified_google_email_creates_nothing": operator_bearer(UNVERIFIED) is None,
        "provider_denial_refused": refused_sign_in(DENIER, "access_denied", deny=True),
        "provider_denial_creates_nothing": operator_bearer(DENIER) is None,
    }


def selection_checks(a, b, session, foreign_org_id):
    """Prove the organization is chosen per request and refusals look the same."""

    def names(org_id, actor_headers=None, workspace=None):
        headers = dict(actor_headers or session)
        headers["X-Organization-Id"] = org_id
        if workspace:
            headers["X-Workspace-ID"] = workspace
        response = request("GET", "/agents", headers)
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


def _identities(bearer):
    """List an account's verified identities through the operator-issued bearer."""
    return httpx.get(API + "/account/me", headers=bearer, timeout=30).json()[
        "identities"
    ]


def _remove(bearer, identity):
    """Ask the real route to detach one identity."""
    return httpx.delete(
        API + "/account/identities/" + identity["id"], headers=bearer, timeout=30
    ).status_code


def identity_checks(a):
    """Prove removing an identity can never leave an account without a verified one."""
    account_session(REMOVAL)
    bearer = operator_bearer(REMOVAL)
    other = operator_bearer(a["shared_person"]["email"])
    assert bearer and other
    listing, foreign = _identities(bearer), _identities(other)
    first, last = listing[0], listing[1]
    stolen = _remove(bearer, foreign[0])
    removed = _remove(bearer, first)
    kept = _remove(bearer, last)
    return {
        "google_sign_in_adds_two_verified_identities": len(listing) == 2,
        "other_account_identity_not_removable": stolen == 404,
        "identity_removed_while_another_verified_remains": removed == 204,
        "last_verified_identity_kept": kept == 409
        and [item["id"] for item in _identities(bearer)] == [last["id"]],
        "other_account_unchanged": len(_identities(other)) == len(foreign),
    }


def creation_checks(a, b, session):
    """Prove an account creates an organization once, idempotently and privately."""
    source = source_for(a["shared_person"]["email"])
    headers = {**session, "Idempotency-Key": "harness-create-1", **source}
    path = "/account/browser/organizations"
    body = {"name": "Harness Created Organization"}
    first = request("POST", path, headers, body)
    replay = request("POST", path, headers, body)
    changed = request("POST", path, headers, {"name": "A Different Name"})
    created = (
        first.json().get("organization", {}).get("id", "")
        if first.status_code == 201
        else ""
    )
    selected = request("GET", "/agents", {**session, "X-Organization-Id": created})
    stranger = account_session(b["user"]["email"])
    refused = request("GET", "/agents", {**stranger, "X-Organization-Id": created})
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
