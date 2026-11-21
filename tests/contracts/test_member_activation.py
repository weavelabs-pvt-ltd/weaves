"""First verified sign-in activates an admin-provisioned (INVITED) member."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import MembershipStatus, PrincipalStatus, UserStatus
from tests.support.account_session import account_headers, sign_in
from tests.support.organizations import onboard
from tests.support.runtime import LocalPlatformRuntime

EMAIL = "pending.member@example.test"


@pytest.fixture
def world():
    runtime = LocalPlatformRuntime(postgres_store=None)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        owner = onboard(
            client,
            name="Activation Org",
            owner_email="owner@activation.test",
            owner_display_name="Owner",
        )
        yield runtime, client, owner
    runtime.close()


def _provision(runtime, client, owner, email=EMAIL):
    """Provision through the admin API, then put the member in its pre-sign-in state."""
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    role = next(
        item
        for item in client.get("/api/v0/roles", headers=owner_headers).json()
        if item["system_role_key"] == "user"
    )
    created = client.post(
        "/api/v0/users",
        headers=owner_headers,
        json={"email": email, "display_name": "Pending", "role_id": role["id"]},
    )
    assert created.status_code == 201, created.text
    user = runtime.users.get(created.json()["user"]["id"])
    principal = runtime.principals.get(created.json()["principal"]["id"])
    return user, principal


def _set_state(runtime, user, principal, user_status, principal_status):
    runtime.users.put(user.model_copy(update={"status": user_status}))
    runtime.principals.put(principal.model_copy(update={"status": principal_status}))


def _audit_actions(runtime, org_id):
    return [event.action for event in runtime.audit_events.list_scoped(org_id)]


def test_invited_member_is_activated_and_can_select_the_organization(world):
    runtime, client, owner = world
    user, principal = _provision(runtime, client, owner)
    _set_state(runtime, user, principal, UserStatus.INVITED, PrincipalStatus.DISABLED)
    org_id = user.org_id
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    before = client.get("/api/v0/users", headers=owner_headers).json()
    assert next(i for i in before if i["id"] == user.id)["account_status"] == "never"

    headers = account_headers(client, EMAIL, org_id)

    assert runtime.users.get(user.id).status is UserStatus.ACTIVE
    assert runtime.principals.get(principal.id).status is PrincipalStatus.ACTIVE
    (membership,) = [
        item for item in runtime.memberships.list() if item.user_id == user.id
    ]
    assert membership.status is MembershipStatus.ACTIVE
    selected = client.get("/api/v0/auth/me", headers=headers)
    assert selected.status_code == 200, selected.text
    assert selected.json()["user"]["id"] == user.id
    actions = _audit_actions(runtime, org_id)
    assert "user.activated_by_email" in actions
    assert "account.membership.linked" in actions
    listing = client.get(
        "/api/v0/users", headers={"Authorization": f"Bearer {owner['access_token']}"}
    )
    row = next(item for item in listing.json() if item["id"] == user.id)
    assert row["account_status"] == "signed_in"


def test_a_second_sign_in_changes_nothing_further(world):
    runtime, client, owner = world
    user, principal = _provision(runtime, client, owner)
    _set_state(runtime, user, principal, UserStatus.INVITED, PrincipalStatus.DISABLED)
    account_headers(client, EMAIL, user.org_id)
    activated = runtime.users.get(user.id)
    events = len(runtime.audit_events.list_scoped(user.org_id))
    account_headers(client, EMAIL, user.org_id)
    assert runtime.users.get(user.id) == activated
    assert len(runtime.audit_events.list_scoped(user.org_id)) == events


def test_a_suspended_user_is_not_reactivated_or_linked(world):
    runtime, client, owner = world
    user, principal = _provision(runtime, client, owner)
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    suspended = client.post(f"/api/v0/users/{user.id}/suspend", headers=owner_headers)
    assert suspended.status_code == 200, suspended.text
    before = (runtime.users.get(user.id), runtime.principals.get(principal.id))
    assert before[0].status is UserStatus.SUSPENDED
    assert before[1].status is PrincipalStatus.DISABLED

    _, token = sign_in(client.app.state.authentication_services.accounts, EMAIL)

    assert (runtime.users.get(user.id), runtime.principals.get(principal.id)) == before
    assert not [m for m in runtime.memberships.list() if m.user_id == user.id]
    denied = client.get(
        "/api/v0/auth/me",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": user.org_id},
    )
    assert denied.status_code == 403
    assert "user.activated_by_email" not in _audit_actions(runtime, user.org_id)


def test_a_deleted_user_is_not_reactivated_or_linked(world):
    runtime, client, owner = world
    user, principal = _provision(runtime, client, owner)
    _set_state(runtime, user, principal, UserStatus.DELETED, PrincipalStatus.DISABLED)
    before = (runtime.users.get(user.id), runtime.principals.get(principal.id))
    sign_in(client.app.state.authentication_services.accounts, EMAIL)
    assert (runtime.users.get(user.id), runtime.principals.get(principal.id)) == before
    assert not [m for m in runtime.memberships.list() if m.user_id == user.id]


def test_an_already_active_member_is_linked_without_being_rewritten(world):
    runtime, client, owner = world
    user, principal = _provision(runtime, client, owner)
    account_headers(client, EMAIL, user.org_id)
    assert runtime.users.get(user.id) == user
    assert runtime.principals.get(principal.id) == principal
    assert "user.activated_by_email" not in _audit_actions(runtime, user.org_id)


def test_activation_rolls_back_with_the_membership_when_the_audit_write_fails(
    world, monkeypatch
):
    runtime, client, owner = world
    user, principal = _provision(runtime, client, owner)
    _set_state(runtime, user, principal, UserStatus.INVITED, PrincipalStatus.DISABLED)
    audit = runtime.account_sign_in_service.linker._audit_events

    def broken(event):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(audit, "create", broken)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        sign_in(client.app.state.authentication_services.accounts, EMAIL)
    assert runtime.users.get(user.id).status is UserStatus.INVITED
    assert runtime.principals.get(principal.id).status is PrincipalStatus.DISABLED
    assert not [m for m in runtime.memberships.list() if m.user_id == user.id]
