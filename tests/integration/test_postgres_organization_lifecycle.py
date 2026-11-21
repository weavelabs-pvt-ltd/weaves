"""Lifecycle, deletion and export on PostgreSQL under the restricted app role."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.auth.organization_retention import PURGE_ORDER
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
)
from tests.support.lifecycle_http import (
    CANCEL,
    EXPORT,
    LIFECYCLE,
    REQUEST,
    SETTINGS,
    drain,
    populated_world,
)
from tests.support.operator_scripts import organization_lifecycle, run_script
from tests.support.organization_administration import headers

OP = ("--operator", "ops@example.test", "--reason", "pg test")
NOT_AVAILABLE = {"detail": "Organization not available"}


def open_runtime(url, role):
    pool = open_postgres_pool(url, min_size=1, max_size=6)
    store = ProductPostgresStore(pool=pool, app_role=role)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    owner = ProductPostgresStore(isolated_postgres_url)
    api, worker = (open_runtime(isolated_postgres_url, "weaves_app") for _ in range(2))
    operator = open_runtime(isolated_postgres_url, "")
    require_tenant_context(True)
    world = populated_world(api)
    world.update(api=api, worker=worker, operator=operator, url=isolated_postgres_url)
    try:
        with ExitStack() as stack:
            world["client"] = stack.enter_context(
                TestClient(create_app(api), raise_server_exceptions=False)
            )
            world["client2"] = stack.enter_context(
                TestClient(create_app(worker), raise_server_exceptions=False)
            )
            yield world
    finally:
        require_tenant_context(False)
        for runtime in (api, worker, operator):
            runtime.close()
        owner.close()


def sql(world, query, *params):
    with psycopg.connect(world["url"], autocommit=True) as connection:
        return connection.execute(query, params).fetchall()


def counts(world, org):
    return dict(
        sql(
            world,
            "SELECT collection, count(*) FROM product_records WHERE org_id = %s "
            "GROUP BY collection",
            org,
        )
    )


def payloads(world, org):
    return sql(
        world,
        "SELECT collection, record_id, payload::text, org_id, workspace_id "
        "FROM product_records WHERE org_id = %s ORDER BY collection, record_id",
        org,
    )


def secrets_of(world, org):
    return sql(world, "SELECT count(*) FROM product_secrets WHERE org_id = %s", org)[0][
        0
    ]


def op(world, command, *extra, org="a"):
    return run_script(
        organization_lifecycle,
        _Capture(),
        world["operator"],
        command,
        "--organization-id",
        world[org][0].id,
        *(OP if command != "status" else ()),
        *extra,
    )[0]


class _Capture:
    """Minimal capsys stand-in: the script's output is not needed here."""

    def readouterr(self):
        return type("Out", (), {"out": "", "err": ""})()


def state(world, org="a"):
    return (
        world["operator"]
        .organization_lifecycle_service.state_of(world[org][0].id)
        .value
    )


def request_deletion(world, client="client"):
    return world[client].post(
        REQUEST, headers=headers(world), json={"confirm_name": "Org A"}
    )


def test_state_transitions_authority_and_recovery_allow_list(world):
    client = world["client"]
    assert (
        client.get(LIFECYCLE, headers=headers(world, "member_token")).status_code == 403
    )
    assert request_deletion(world).status_code == 200
    assert state(world) == "deletion_requested"
    assert client.get(SETTINGS, headers=headers(world)).json() == NOT_AVAILABLE
    assert client.get(LIFECYCLE, headers=headers(world)).status_code == 200
    for token, tenant in (("member_token", "a"), ("b_token", "a")):
        r = client.post(CANCEL, headers=headers(world, token, tenant))
        assert (r.status_code, r.json()) == (403, NOT_AVAILABLE)
    assert client.post(CANCEL, headers=headers(world)).json()["state"] == "active"
    assert op(world, "suspend") == 0 and state(world) == "suspended"
    assert client.post(CANCEL, headers=headers(world)).status_code == 403
    assert (
        client.get(SETTINGS, headers=headers(world, "b_token", "b")).status_code == 200
    )
    assert op(world, "resume") == 0 and state(world) == "active"
    assert client.get(SETTINGS, headers=headers(world)).status_code == 200
    me = client.get(
        "/api/v0/account/me",
        headers={"Authorization": "Bearer " + world["owner_token"]},
    ).json()
    assert me["memberships"][0]["lifecycle_state"] == "active"


def test_restricted_role_cannot_see_other_tenants_lifecycle_or_data(world):
    org_b = world["b"][0].id
    token = set_request_identity("p", world["a"][0].id, "w")
    try:
        assert world["api"].organization_lifecycle.list_scoped(org_b) == ()
        data = world["api"]._repository_set.organization_data
        assert data.page("tasks", org_b, after=None, limit=10) == []
        assert data.delete_batch("tasks", org_b, limit=10) == 0
    finally:
        reset_request_principal(token)
    assert counts(world, org_b)["tasks"] == 1


def test_full_deletion_purges_everything_but_the_retained_set(world):
    a, b = world["a"][0].id, world["b"][0].id
    before_b, secrets_b = payloads(world, b), secrets_of(world, b)
    assert secrets_of(world, a) > 0 and counts(world, a)["execution_jobs"] == 1
    assert request_deletion(world).status_code == 200
    assert op(world, "purge-now", "--confirm-organization-name", "Org A") == 0
    drain(world["worker"])
    assert state(world) == "deleted"
    left = counts(world, a)
    assert set(left) == {"organization_lifecycle", "audit_events"}
    actions = [
        r[0]
        for r in sql(
            world,
            "SELECT payload->>'action' FROM product_records "
            "WHERE collection='audit_events' AND org_id=%s",
            a,
        )
    ]
    assert actions and all(x.startswith("organization.lifecycle.") for x in actions)
    assert secrets_of(world, a) == 0
    assert payloads(world, b) == before_b and secrets_of(world, b) == secrets_b
    assert (
        sql(
            world,
            "SELECT count(*) FROM product_records WHERE collection="
            "'memberships' AND org_id=%s",
            a,
        )[0][0]
        == 0
    )
    assert world["operator"].accounts.get(world["member_account"].id)
    r = world["client"].get(SETTINGS, headers=headers(world))
    assert (r.status_code, r.json()) == (403, NOT_AVAILABLE)
    assert (
        world["client"]
        .get(SETTINGS, headers=headers(world, "b_token", "b"))
        .status_code
        == 200
    )
    assert op(world, "request-deletion") == 0 and state(world) == "deleted"
    assert op(world, "resume") == 2


@pytest.mark.parametrize(
    "step",
    ["begin", "cancel_work", "revoke_credentials", "secrets", "execution_jobs"]
    + [PURGE_ORDER[i] for i in (3, 9, 24, 35, 36)]
    + ["audit_events", "sweep", "finalize"],
)
def test_crash_at_each_step_converges_with_restricted_role(world, step):
    a, b = world["a"][0].id, world["b"][0].id
    before_b = payloads(world, b)
    assert request_deletion(world).status_code == 200
    assert op(world, "purge-now", "--confirm-organization-name", "Org A") == 0
    service = world["worker"].organization_purge_service
    crashed = []

    def hook(name):
        if name == step and not crashed:
            crashed.append(name)
            raise RuntimeError("crash")

    service.hook = hook
    for _ in range(6):
        service.tick()
        if crashed:
            break
    assert crashed == [step]
    service.hook = None
    drain(world["worker"])
    assert state(world) == "deleted"
    assert set(counts(world, a)) == {"organization_lifecycle", "audit_events"}
    assert secrets_of(world, a) == 0 and payloads(world, b) == before_b


def race(*operations):
    barrier = Barrier(len(operations))

    def run(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def test_two_simultaneous_deletion_requests_have_one_effect(world):
    results = race(
        lambda: request_deletion(world, "client"),
        lambda: request_deletion(world, "client2"),
    )
    assert sorted(r.status_code for r in results)[0] == 200
    assert {r.status_code for r in results} <= {200, 403}
    events = sql(
        world,
        "SELECT payload->>'action' FROM product_records WHERE collection="
        "'audit_events' AND org_id=%s AND payload->>'action' = "
        "'organization.lifecycle.deletion_requested'",
        world["a"][0].id,
    )
    assert len(events) == 1 and state(world) == "deletion_requested"


def test_cancel_racing_operator_purge_never_leaves_a_mixed_organization(world):
    a = world["a"][0].id
    assert request_deletion(world).status_code == 200
    cancel, purge = race(
        lambda: world["client"].post(CANCEL, headers=headers(world)),
        lambda: op(world, "purge-now", "--confirm-organization-name", "Org A"),
    )
    drain(world["worker"])
    if state(world) == "active":
        assert cancel.status_code == 200 and purge == 2
        assert counts(world, a).get("tasks") == 1
    else:
        assert state(world) == "deleted" and purge == 0
        assert cancel.status_code in (403, 409)
        assert set(counts(world, a)) == {"organization_lifecycle", "audit_events"}


def test_purge_racing_a_new_login_leaves_no_membership(world):
    from shared.request_context import system_scope
    from tests.support.account_session import sign_in
    from tests.support.organization_administration import scope

    runtime, a = world["api"], world["a"]
    with scope(a):
        role = next(
            r for r in runtime.roles.list_scoped(a[0].id) if r.system_role_key == "user"
        )
        runtime.user_administration_service.create_workspace_user(
            email="late@example.test", display_name="Late", role_id=role.id
        )
    assert request_deletion(world).status_code == 200
    assert op(world, "purge-now", "--confirm-organization-name", "Org A") == 0

    def login():
        with system_scope("account"):
            sign_in(runtime.account_sign_in_service, "late@example.test")

    race(login, lambda: drain(world["worker"]), lambda: drain(world["worker"]))
    drain(world["worker"])
    assert state(world) == "deleted"
    assert counts(world, a[0].id).get("memberships") is None


def test_export_on_postgres_excludes_secrets_and_other_tenants(world):
    response = world["client"].post(EXPORT, headers=headers(world))
    assert response.status_code == 200, response.text
    body = response.text
    assert "BRAVO" not in body and "ALPHA" in body
    for secret in world["seed_a"]["secrets"] + world["seed_b"]["secrets"]:
        assert secret not in body
    document = json.loads(body)
    assert document["organization"]["id"] == world["a"][0].id
    assert (
        world["client"].post(EXPORT, headers=headers(world, "member_token")).status_code
        == 403
    )
