"""Two independent API runtimes serialize tenant administration through PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier, Event

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import UserStatus
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.auth.organization_guard import OWNER_PERMISSIONS
from shared.request_context import require_tenant_context
from tests.support.organization_administration import (
    TRANSFER,
    URL,
    headers,
    prepare,
    scope,
    transfer_body,
)


@pytest.fixture
def world(isolated_postgres_url):
    owner = ProductPostgresStore(isolated_postgres_url)
    runtimes = []
    for _ in range(2):
        pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=6)
        store = ProductPostgresStore(pool=pool, app_role="weaves_app")
        runtime = compose_platform_runtime(
            http_client=httpx.Client(), store=store, seed_local_defaults=False
        )
        runtime.bootstrap()
        runtimes.append(runtime)
    require_tenant_context(True)
    world = {"runtimes": runtimes, **prepare(runtimes[0])}
    try:
        with ExitStack() as stack:
            world["clients"] = [
                stack.enter_context(
                    TestClient(create_app(r), raise_server_exceptions=False)
                )
                for r in runtimes
            ]
            yield world
    finally:
        require_tenant_context(False)
        for runtime in runtimes:
            runtime.close()
        owner.close()


def race(*operations):
    """Start independent API requests together, retaining both actual responses."""
    barrier = Barrier(len(operations))

    def run(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def assert_active_owner(world):
    """Check stored ownership, active human lifecycle and Admin permissions."""
    runtime = world["runtimes"][0]
    with scope(world["a"]):
        guard = runtime.organization_administration_service.guard
        owner = guard.current_settings().owner_principal_id
        assert owner in guard.active_humans()
        assert OWNER_PERMISSIONS.issubset(
            guard.permissions(guard.bindings.list_scoped(world["a"][0].id))[owner]
        )


def test_simultaneous_preferences_edits_cannot_overwrite_one_another(world):
    payload = {
        "name": "Edited A",
        "timezone": "Europe/London",
        "locale": "en-GB",
        "expected_revision": 1,
    }
    responses = race(
        *(
            lambda client=c: client.patch(URL, headers=headers(world), json=payload)
            for c in world["clients"]
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (
        world["clients"][0]
        .get(URL, headers=headers(world))
        .json()["settings"]["revision"]
        == 2
    )
    assert_active_owner(world)


def test_two_transfers_from_the_same_owner_have_one_winner(world):
    responses = race(
        *(
            lambda client=c: client.post(
                TRANSFER, headers=headers(world), json=transfer_body(world)
            )
            for c in world["clients"]
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert_active_owner(world)


@pytest.mark.parametrize("operation", ["suspend", "remove", "remove_admin"])
def test_transfer_racing_access_revocation_preserves_an_active_owner(world, operation):
    a, b = world["clients"]
    user, principal, _ = world["member_a"]
    runtime = world["runtimes"][0]
    # An extra Admin makes role removal legal before the transfer, but never after it.
    with scope(world["a"]):
        admin = next(
            r
            for r in runtime.roles.list_scoped(world["a"][0].id)
            if r.system_role_key == "admin"
        )
        binding = runtime.workspace_access_service.create_role_binding(
            principal_id=principal.id, role_id=admin.id
        )

    def revoke():
        if operation == "suspend":
            return b.post(f"/api/v0/users/{user.id}/suspend", headers=headers(world))
        if operation == "remove":
            return b.delete(
                f"/api/v0/organization/members/{user.id}", headers=headers(world)
            )
        return b.delete(f"/api/v0/role-bindings/{binding.id}", headers=headers(world))

    transferred, revoked = race(
        lambda: a.post(TRANSFER, headers=headers(world), json=transfer_body(world)),
        revoke,
    )
    if transferred.status_code == 200:
        assert revoked.status_code in (204, 409), revoked.text
        # If removal of the role won first, transfer must re-grant Admin atomically.
        if operation != "remove_admin":
            assert revoked.status_code == 409
    else:
        assert transferred.status_code == 409, transferred.text
        assert revoked.status_code in (200, 204), revoked.text
    assert_active_owner(world)
    with scope(world["b"]):
        assert (
            runtime.users.get_scoped(world["member_b"][0].id, world["b"][0].id).status
            is UserStatus.ACTIVE
        )


def test_authorization_is_rechecked_after_waiting_for_the_org_lock(world):
    runtime, other = world["runtimes"]
    principal = world["member_a"][1]
    with scope(world["a"]):
        admin = next(
            r
            for r in runtime.roles.list_scoped(world["a"][0].id)
            if r.system_role_key == "admin"
        )
        binding = runtime.workspace_access_service.create_role_binding(
            principal_id=principal.id, role_id=admin.id
        )
    waiting, resume = Event(), Event()
    guard = other.organization_administration_service.guard
    original = guard.lock

    def delayed_lock(name):
        waiting.set()
        assert resume.wait(timeout=15)
        original(name)

    guard.lock = delayed_lock
    payload = {
        "name": "Revoked actor edit",
        "timezone": "UTC",
        "locale": "en",
        "expected_revision": 1,
    }
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(
                lambda: world["clients"][1].patch(
                    URL, headers=headers(world, "member_token"), json=payload
                )
            )
            try:
                assert waiting.wait(timeout=10)
                removed = world["clients"][0].delete(
                    f"/api/v0/role-bindings/{binding.id}", headers=headers(world)
                )
                assert removed.status_code == 204
            finally:
                resume.set()
            assert attempt.result(timeout=15).status_code == 403
    finally:
        guard.lock = original
    assert (
        world["clients"][0]
        .get(URL, headers=headers(world))
        .json()["organization"]["name"]
        == "Org A"
    )
    assert_active_owner(world)


def test_failed_transfer_rolls_back_owner_role_and_audit_on_postgres(
    world, monkeypatch
):
    runtime = world["runtimes"][0]
    original = runtime.audit_events.create

    def fail(event):
        if event.action == "organization.ownership_transferred":
            raise RuntimeError("Injected database transaction failure")
        return original(event)

    monkeypatch.setattr(runtime.audit_events, "create", fail)
    response = world["clients"][0].post(
        TRANSFER, headers=headers(world), json=transfer_body(world)
    )
    assert response.status_code == 500
    state = world["clients"][1].get(URL, headers=headers(world)).json()
    assert state["settings"]["revision"] == 1
    assert state["owner"]["principal_id"] == world["a"][3].id
    assert (
        world["clients"][1].get(URL, headers=headers(world, "member_token")).status_code
        == 403
    )
    assert_active_owner(world)
