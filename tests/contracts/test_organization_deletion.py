"""Resumable deletion: purge scope, retention, terminal state and crash safety."""

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import AccountOrganizationRequest
from contracts.v1 import OrganizationLifecycleState as State
from modules.auth.organization_retention import PURGE_ORDER
from shared.clock import utc_now
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.lifecycle_http import (
    LIFECYCLE,
    REQUEST,
    SETTINGS,
    drain,
    populated_world,
)
from tests.support.lifecycle_world import digest, rows
from tests.support.operator_scripts import organization_lifecycle, run_script
from tests.support.organization_administration import headers, scope

OP = ("--operator", "ops@example.test", "--reason", "test")
STEPS = [
    "begin",
    "cancel_work",
    "revoke_credentials",
    "slack",
    "secrets",
    *PURGE_ORDER,
    "audit_events",
    "sweep",
    "finalize",
]


@pytest.fixture
def world():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    world = populated_world(runtime)
    with system_scope("account"):
        runtime.account_organization_requests.create(
            AccountOrganizationRequest(
                id="a" * 64,
                account_id=world["member_account"].id,
                key_digest="b" * 64,
                request_fingerprint="c" * 64,
                organization_id=world["a"][0].id,
                created_at=utc_now(),
            )
        )
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        world["client"] = client
        yield world
    runtime.close()


def op(world, capsys, command, *extra):
    return run_script(
        organization_lifecycle,
        capsys,
        world["runtime"],
        command,
        "--organization-id",
        world["a"][0].id,
        *(OP if command != "status" else ()),
        *extra,
    )


def request_and_purge(world, capsys):
    client = world["client"]
    r = client.post(REQUEST, headers=headers(world), json={"confirm_name": "Org A"})
    assert r.status_code == 200, r.text
    assert (
        op(world, capsys, "purge-now", "--confirm-organization-name", "Org A")[0] == 0
    )


def state(world):
    return world["runtime"].organization_lifecycle_service.state_of(world["a"][0].id)


def test_purge_removes_everything_except_the_documented_retained_set(world, capsys):
    runtime, org = world["runtime"], world["a"][0].id
    before_b = digest(rows(runtime, world["b"][0].id))
    export = world["client"].post("/api/v0/organization/export", headers=headers(world))
    assert export.status_code == 200
    assert rows(runtime, org, keep=False), "fixture must own rows"
    request_and_purge(world, capsys)
    drain(runtime)
    assert state(world) is State.DELETED
    left = rows(runtime, org)
    assert set(left) == {"organization_lifecycle", "audit_events"}
    audit = [e for e in runtime.audit_events.list_scoped(org)]
    assert audit and all(
        e.action.startswith("organization.lifecycle.")
        or e.action == "organization.exported"
        for e in audit
    )
    assert any(e.action == "organization.lifecycle.deleted" for e in audit)
    organization = runtime.organizations.get(org)
    assert organization.name == "deleted organization"
    record = runtime.organization_lifecycle.get(org)
    assert record.deleted_at and record.state is State.DELETED
    assert [h.state.value for h in record.history] == [
        "deletion_requested",
        "deleting",
        "deleted",
    ]
    assert digest(rows(runtime, world["b"][0].id)) == before_b
    token = set_request_identity("x", org, "w")
    try:
        for secret_ref in world["seed_a"]["secrets"]:
            with pytest.raises(KeyError):
                runtime.secret_store.get(secret_ref)
    finally:
        reset_request_principal(token)
    with system_scope("account"):
        assert runtime.account_organization_requests.list(), "quota ledger retained"
        accounts = runtime.memberships.list_by_field(
            "account_id", world["member_account"].id
        )
    assert {m.org_id for m in accounts} == {world["b"][0].id}
    assert runtime.accounts.get(world["member_account"].id)


def test_second_organization_secret_and_credential_survive(world, capsys):
    runtime = world["runtime"]
    request_and_purge(world, capsys)
    drain(runtime)
    b = world["b"]
    with scope(b):
        refs = [s for s in world["seed_b"]["secrets"] if s.startswith("secret:")]
        assert refs and runtime.secret_store.get(refs[0])
    r = world["client"].get(SETTINGS, headers=headers(world, "b_token", "b"))
    assert r.status_code == 200


def test_deleted_is_terminal_and_repeated_deletion_is_a_no_op(world, capsys):
    runtime, client = world["runtime"], world["client"]
    request_and_purge(world, capsys)
    drain(runtime)
    snapshot = digest(rows(runtime, world["a"][0].id))
    for command in ("resume", "suspend", "cancel-deletion"):
        assert op(world, capsys, command)[0] in (0, 2)
        assert state(world) is State.DELETED
    assert op(world, capsys, "request-deletion")[0] == 0
    assert op(world, capsys, "purge-now", "--confirm-organization-name", "x")[0] == 0
    assert state(world) is State.DELETED
    for path in (SETTINGS, LIFECYCLE):
        r = client.get(path, headers=headers(world))
        assert (r.status_code, r.json()) == (
            403,
            {"detail": "Organization not available"},
        )
    me = client.get(
        "/api/v0/account/me",
        headers={"Authorization": "Bearer " + world["owner_token"]},
    ).json()
    assert me["memberships"] == []
    assert digest(rows(runtime, world["a"][0].id)) == snapshot
    status = op(world, capsys, "status")
    assert status[0] == 0 and '"remaining_rows": 0' in status[1]


def test_grace_period_must_elapse_before_the_worker_deletes(world):
    runtime, client = world["runtime"], world["client"]
    client.post(REQUEST, headers=headers(world), json={"confirm_name": "Org A"})
    drain(runtime)
    assert state(world) is State.DELETION_REQUESTED
    assert rows(runtime, world["a"][0].id, keep=False)
    runtime.organization_lifecycle_service.now = lambda: utc_now() + timedelta(days=8)
    drain(runtime)
    assert state(world) is State.DELETED
    runtime.organization_lifecycle_service.now = utc_now


@pytest.mark.parametrize("step", STEPS)
def test_crash_at_every_step_converges_on_retry(world, capsys, step):
    runtime, org = world["runtime"], world["a"][0].id
    before_b = digest(rows(runtime, world["b"][0].id))
    request_and_purge(world, capsys)
    service = runtime.organization_purge_service
    crashed = []

    def hook(name):
        if name == step and not crashed:
            crashed.append(name)
            raise RuntimeError("simulated crash at " + name)

    service.hook = hook
    for _ in range(6):
        service.tick()
        if crashed:
            break
    assert crashed == [step], "the step must be reached"
    assert state(world) in (State.DELETION_REQUESTED, State.DELETING)
    service.hook = None
    drain(runtime)
    assert state(world) is State.DELETED
    assert set(rows(runtime, org)) == {"organization_lifecycle", "audit_events"}
    assert digest(rows(runtime, world["b"][0].id)) == before_b


def test_deletion_tick_failure_never_raises_into_the_worker_loop(world, capsys):
    request_and_purge(world, capsys)
    service = world["runtime"].organization_purge_service

    def boom(name):
        raise RuntimeError("always failing")

    service.hook = boom
    assert service.tick() == 0  # logged and swallowed
    service.hook = None


def test_new_login_cannot_link_into_a_deleting_organization(world, capsys):
    from tests.support.account_session import sign_in

    runtime, a = world["runtime"], world["a"]
    with scope(a):
        role = next(
            r for r in runtime.roles.list_scoped(a[0].id) if r.system_role_key == "user"
        )
        runtime.user_administration_service.create_workspace_user(
            email="late@example.test", display_name="Late", role_id=role.id
        )
    request_and_purge(world, capsys)
    with system_scope("account"):
        account, _ = sign_in(runtime.account_sign_in_service, "late@example.test")
        linked = runtime.memberships.list_by_field("account_id", account.id)
    assert not linked
    drain(runtime)
    assert state(world) is State.DELETED


def test_queued_work_of_a_non_active_organization_is_not_executed(world, capsys):
    runtime, org = world["runtime"], world["a"][0].id
    runs_before = len(runtime.runs.list_scoped(org))
    assert op(world, capsys, "suspend")[0] == 0
    for _ in range(5):
        runtime.process_next_execution_job("worker-lifecycle")
    assert len(runtime.runs.list_scoped(org)) == runs_before
    statuses = {j.status.value for j in runtime.execution_jobs.list_scoped(org)}
    assert "succeeded" not in statuses and "running" not in statuses


def test_purge_completion_frees_the_slug_and_the_email_domain(world, capsys):
    runtime, org = world["runtime"], world["a"][0].id
    with system_scope("lifecycle"):
        held = runtime.organization_settings.get_scoped(org, org)
        runtime.organization_settings.put(
            held.model_copy(update={"slug": "gone-soon", "email_domain": "gone.test"})
        )
        assert runtime.organization_settings.list_by_field(
            "slug", "GONE-SOON", casefold=True
        )
    request_and_purge(world, capsys)
    drain(runtime)
    with system_scope("lifecycle"):
        assert not runtime.organization_settings.list_by_field(
            "slug", "gone-soon", casefold=True
        )
        assert not runtime.organization_settings.list_by_field(
            "email_domain", "gone.test", casefold=True
        )
    assert runtime.account_organization_service.domains.claim_of("gone.test") is None
