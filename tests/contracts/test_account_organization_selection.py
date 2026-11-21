"""An account acts only inside an organization it currently belongs to."""

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import (
    MembershipStatus,
    OrganizationStatus,
    PrincipalStatus,
    UserStatus,
)
from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from tests.support.account_world import HMAC_KEY, CapturedMail
from tests.support.organizations import create_organization

SHARED = "shared.person@example.test"


@pytest.fixture
def world():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    service = runtime.account_sign_in_service
    mail = CapturedMail()
    service.email_sender = mail
    service.email_login_hmac_key = HMAC_KEY
    service.login_rate_limiter = InMemoryLoginRateLimiter(limit=50)
    org_a, ws_a, user_a, principal_a, owner_a = create_organization(
        runtime, name="Org A", email=SHARED, display_name="Shared A"
    )
    org_b, ws_b, user_b, principal_b, owner_b = create_organization(
        runtime, name="Org B", email=SHARED, display_name="Shared B"
    )
    org_c, _, _, _, _ = create_organization(
        runtime, name="Org C", email="stranger@example.test", display_name="Stranger"
    )
    challenge = service.start_email_login(SHARED, "198.51.100.1")
    _, _, token = service.verify_email_login(challenge, mail.code(), "198.51.100.1")
    client = TestClient(create_app(runtime), raise_server_exceptions=False)
    with client:
        yield {
            "runtime": runtime,
            "client": client,
            "token": token,
            "a": (org_a, ws_a, user_a, principal_a, owner_a),
            "b": (org_b, ws_b, user_b, principal_b, owner_b),
            "c": org_c,
        }
    runtime.close()


def _get(world, path="/api/v0/agents", *, org=None, workspace=None, token=None):
    headers = {"Authorization": "Bearer " + (token or world["token"])}
    if org:
        headers["X-Organization-Id"] = org
    if workspace:
        headers["X-Workspace-ID"] = workspace
    return world["client"].get(path, headers=headers)


def test_one_account_works_in_each_member_organization_as_its_local_principal(world):
    runtime = world["runtime"]
    a, b = world["a"], world["b"]
    for org, *_ in (a, b):
        agent_name = f"Agent in {org.name}"
        owner_token = world["a"][4] if org is a[0] else world["b"][4]
        created = world["client"].post(
            "/api/v0/agents",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={"name": agent_name, "instructions": "Say hello"},
        )
        assert created.status_code in (200, 201), created.text
        listed = _get(world, org=org.id)
        assert listed.status_code == 200, listed.text
        assert [item["name"] for item in listed.json()] == [agent_name]
    audit = [event for event in runtime.audit_events.list() if event.org_id == a[0].id]
    assert any(event.actor_principal_id == a[3].id for event in audit)


def test_missing_unknown_and_foreign_organizations_are_indistinguishable(world):
    missing = _get(world)
    unknown = _get(world, org="no-such-organization")
    foreign = _get(world, org=world["c"].id)  # a real organization, not a member
    assert missing.status_code == 401
    assert (unknown.status_code, unknown.json()) == (
        foreign.status_code,
        foreign.json(),
    )
    assert unknown.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        "membership_removed",
        "membership_suspended",
        "user_disabled",
        "principal_disabled",
        "organization_suspended",
    ],
)
def test_access_stops_with_current_state_and_never_leaks_to_other_organizations(
    world, change
):
    runtime = world["runtime"]
    org_a, _, user_a, principal_a, _ = world["a"]
    org_b = world["b"][0]
    assert _get(world, org=org_a.id).status_code == 200
    if change.startswith("membership"):
        membership = next(
            m
            for m in runtime.memberships.list_scoped(org_a.id)
            if m.principal_id == principal_a.id
        )
        status = (
            MembershipStatus.REMOVED
            if change.endswith("removed")
            else MembershipStatus.SUSPENDED
        )
        runtime.memberships.put(membership.model_copy(update={"status": status}))
    elif change == "user_disabled":
        runtime.users.put(user_a.model_copy(update={"status": UserStatus.SUSPENDED}))
    elif change == "principal_disabled":
        runtime.principals.put(
            principal_a.model_copy(update={"status": PrincipalStatus.DISABLED})
        )
    else:
        runtime.organizations.put(
            org_a.model_copy(update={"status": OrganizationStatus.SUSPENDED})
        )

    assert _get(world, org=org_a.id).status_code == 403
    assert _get(world, org=org_b.id).status_code == 200  # B is unaffected


def test_workspace_selection_is_validated_inside_the_selected_organization(world):
    org_a, ws_a = world["a"][0], world["a"][1]
    ws_b = world["b"][1]
    assert _get(world, org=org_a.id, workspace=ws_a.id).status_code == 200
    assert _get(world, org=org_a.id, workspace=ws_b.id).status_code == 403


def test_other_credentials_ignore_the_organization_header(world):
    org_a, org_b = world["a"][0], world["b"][0]
    owner_a = world["a"][4]
    seen = world["client"].get(
        "/api/v0/agents",
        headers={"Authorization": f"Bearer {owner_a}", "X-Organization-Id": org_b.id},
    )
    assert seen.status_code == 200  # owner A's own tenant token, header ignored
    assert (
        world["client"]
        .get(
            "/api/v0/agents",
            headers={
                "Authorization": f"Bearer {owner_a}",
                "X-Organization-Id": org_a.id,
            },
        )
        .json()
        == seen.json()
    )


def test_an_account_session_never_reaches_account_routes_as_a_tenant(world):
    me = world["client"].get(
        "/api/v0/account/me",
        headers={
            "Authorization": "Bearer " + world["token"],
            "X-Organization-Id": world["a"][0].id,
        },
    )
    assert me.status_code == 200  # account route: header irrelevant, no tenant bound
    assert {m["organization_id"] for m in me.json()["memberships"]} == {
        world["a"][0].id,
        world["b"][0].id,
    }
