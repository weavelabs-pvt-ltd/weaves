"""Organization settings, explicit ownership and tenant-local membership revocation."""

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import UserStatus
from tests.support.organization_administration import (
    TRANSFER,
    URL,
    headers,
    prepare,
    scope,
    transfer_body,
)


@pytest.fixture
def world():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    world = {"runtime": runtime, **prepare(runtime)}
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        world["client"] = client
        yield world
    runtime.close()


def test_settings_are_scoped_validated_versioned_and_audited(world):
    client, h = world["client"], headers(world)
    initial = client.get(URL, headers=h)
    assert initial.status_code == 200, initial.text
    assert initial.json()["owner"]["principal_id"] == world["a"][3].id
    assert initial.json()["settings"]["timezone"] == "Asia/Kolkata"
    payload = {
        "name": "Renamed A",
        "timezone": "America/New_York",
        "locale": "en-US",
        "expected_revision": 1,
    }
    response = client.patch(URL, headers=h, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["revision"] == 2
    assert response.json()["organization"]["name"] == "Renamed A"
    assert client.patch(URL, headers=h, json=payload).status_code == 409
    for changes in (
        {"timezone": "Mars/Phobos"},
        {"locale": "not valid"},
        {"name": "bad\x00name"},
        {"owner_principal_id": "other"},
    ):
        assert (
            client.patch(
                URL, headers=h, json={**payload, "expected_revision": 2, **changes}
            ).status_code
            == 422
        )
    other = client.get(URL, headers=headers(world, "b_token", "b")).json()
    assert other["organization"]["name"] == "Org B"
    with scope(world["a"]):
        events = world["runtime"].audit_events.list_scoped(world["a"][0].id)
    assert sum(e.action == "organization.settings_updated" for e in events) == 1


def test_member_cannot_discover_settings_or_manage_other_tenant(world):
    client = world["client"]
    assert client.get(URL, headers=headers(world, "member_token")).status_code == 403
    assert (
        client.get(URL, headers=headers(world, "owner_token", "b")).status_code == 403
    )
    wrong = transfer_body(world, principal_id=world["b"][3].id)
    assert client.post(TRANSFER, headers=headers(world), json=wrong).status_code == 404


def test_transfer_is_confirmed_atomic_and_only_the_current_owner_can_repeat(world):
    client = world["client"]
    assert (
        client.post(
            TRANSFER,
            headers=headers(world),
            json=transfer_body(world, confirmation="wrong"),
        ).status_code
        == 422
    )
    response = client.post(TRANSFER, headers=headers(world), json=transfer_body(world))
    assert response.status_code == 200, response.text
    assert response.json()["owner"]["principal_id"] == world["member_a"][1].id
    assert response.json()["settings"]["revision"] == 2
    assert response.json()["can_transfer"] is False
    assert (
        client.get(URL, headers=headers(world, "member_token")).json()["can_transfer"]
        is True
    )
    # The former owner retains Admin, but cannot transfer again from a fresh form.
    assert client.get(URL, headers=headers(world)).status_code == 200
    assert (
        client.post(
            TRANSFER,
            headers=headers(world),
            json=transfer_body(world, expected_revision=2),
        ).status_code
        == 403
    )
    assert (
        client.get(URL, headers=headers(world, "member_token", "b")).status_code == 403
    )


def test_owner_cannot_be_suspended_removed_or_demoted(world):
    client, runtime, owner = world["client"], world["runtime"], world["a"]
    assert (
        client.post(
            f"/api/v0/users/{owner[2].id}/suspend", headers=headers(world)
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/api/v0/organization/members/{owner[2].id}", headers=headers(world)
        ).status_code
        == 409
    )
    with scope(owner):
        binding = next(
            b
            for b in runtime.role_bindings.list_scoped(owner[0].id)
            if b.principal_id == owner[3].id
        )
    assert (
        client.delete(
            f"/api/v0/role-bindings/{binding.id}", headers=headers(world)
        ).status_code
        == 409
    )
    assert client.get(URL, headers=headers(world)).status_code == 200


def test_removal_preserves_global_account_other_tenant_and_business_records(world):
    client, runtime = world["client"], world["runtime"]
    member = world["member_a"][0]
    url = f"/api/v0/organization/members/{member.id}"
    assert client.delete(url, headers=headers(world)).status_code == 204
    assert client.delete(url, headers=headers(world)).status_code == 204
    assert (
        client.get(
            "/api/v0/auth/me", headers=headers(world, "member_token")
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v0/auth/me", headers=headers(world, "member_token", "b")
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v0/account/me",
            headers={"Authorization": "Bearer " + world["member_token"]},
        ).status_code
        == 200
    )
    assert member.id not in {
        u["id"] for u in client.get("/api/v0/users", headers=headers(world)).json()
    }
    with scope(world["a"]):
        assert (
            runtime.users.get_scoped(member.id, member.org_id).status
            is UserStatus.DELETED
        )
        assert (
            runtime.users.get_scoped(world["a"][2].id, member.org_id).status
            is UserStatus.ACTIVE
        )


def test_legacy_owner_is_proven_by_creation_audit_and_read_does_not_backfill(world):
    runtime, org = world["runtime"], world["a"][0]
    with scope(world["a"]):
        runtime.organization_settings.delete(org.id)
    response = world["client"].get(URL, headers=headers(world))
    assert response.status_code == 200, response.text
    assert response.json()["owner"]["principal_id"] == world["a"][3].id
    with scope(world["a"]):
        assert not runtime.organization_settings.list_scoped(org.id)
        for event in runtime.audit_events.list_scoped(org.id):
            if event.action == "organization.created":
                runtime.audit_events.delete(event.audit_event_id)
    assert world["client"].get(URL, headers=headers(world)).status_code == 409


@pytest.mark.parametrize("state", ["user", "principal", "membership", "account"])
def test_transfer_recipient_must_have_current_active_access(world, state):
    from contracts.v1 import AccountStatus, MembershipStatus, PrincipalStatus
    from shared.request_context import system_scope

    runtime, member = world["runtime"], world["member_a"]
    with scope(world["a"]):
        if state == "user":
            runtime.users.put(
                member[0].model_copy(update={"status": UserStatus.SUSPENDED})
            )
        elif state == "principal":
            runtime.principals.put(
                member[1].model_copy(update={"status": PrincipalStatus.DISABLED})
            )
        elif state == "membership":
            link = next(
                m
                for m in runtime.memberships.list_scoped(world["a"][0].id)
                if m.principal_id == member[1].id
            )
            runtime.memberships.put(
                link.model_copy(update={"status": MembershipStatus.SUSPENDED})
            )
        else:
            with system_scope("account"):
                runtime.accounts.put(
                    world["member_account"].model_copy(
                        update={"status": AccountStatus.DISABLED}
                    )
                )
    response = world["client"].post(
        TRANSFER, headers=headers(world), json=transfer_body(world)
    )
    assert response.status_code == 409
    assert (
        world["client"].get(URL, headers=headers(world)).json()["owner"]["principal_id"]
        == world["a"][3].id
    )


@pytest.mark.parametrize("operation", ["settings", "transfer"])
def test_audit_failure_rolls_back_preferences_roles_and_ownership(
    world, monkeypatch, operation
):
    runtime, client = world["runtime"], world["client"]
    original = runtime.audit_events.create

    def fail(event):
        if event.action in {
            "organization.settings_updated",
            "organization.ownership_transferred",
        }:
            raise RuntimeError("Injected audit failure")
        return original(event)

    monkeypatch.setattr(runtime.audit_events, "create", fail)
    if operation == "transfer":
        response = client.post(
            TRANSFER, headers=headers(world), json=transfer_body(world)
        )
    else:
        response = client.patch(
            URL,
            headers=headers(world),
            json={
                "name": "Must roll back",
                "timezone": "UTC",
                "locale": "en",
                "expected_revision": 1,
            },
        )
    assert response.status_code == 500
    state = client.get(URL, headers=headers(world)).json()
    assert state["organization"]["name"] == "Org A"
    assert state["settings"]["revision"] == 1
    assert state["owner"]["principal_id"] == world["a"][3].id
    assert client.get(URL, headers=headers(world, "member_token")).status_code == 403


def test_tenant_api_credentials_cannot_transfer_human_ownership(world):
    owner = world["a"]
    response = world["client"].post(
        TRANSFER,
        json=transfer_body(world),
        headers={"Authorization": "Bearer " + owner[4]},
    )
    assert response.status_code == 403
