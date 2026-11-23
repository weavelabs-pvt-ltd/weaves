"""Owner and operator lifecycle authority, the recovery allow-list and no-replay."""

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import OrganizationLifecycleState as State
from shared.clock import utc_now
from tests.support.lifecycle_http import (
    CANCEL,
    EXPORT,
    LIFECYCLE,
    REQUEST,
    SETTINGS,
    populated_world,
)
from tests.support.operator_scripts import organization_lifecycle, run_script
from tests.support.organization_administration import headers, scope

OP = ("--operator", "ops@example.test", "--reason", "test")
NOT_AVAILABLE = {"detail": "Organization not available"}
TRANSFER = "/api/v0/organization/transfer-ownership"


@pytest.fixture
def world():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    world = populated_world(runtime)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        world["client"] = client
        yield world
    runtime.close()


def operator(world, capsys, command, *extra):
    org = world["a"][0].id
    return run_script(
        organization_lifecycle,
        capsys,
        world["runtime"],
        command,
        "--organization-id",
        org,
        *(OP if command != "status" else ()),
        *extra,
    )


def state(world):
    return world["runtime"].organization_lifecycle_service.state_of(world["a"][0].id)


def bearer(world, token="owner_token"):
    return {"Authorization": "Bearer " + world[token]}


def lifecycle_actions(world):
    return [
        e
        for e in world["runtime"].audit_events.list_scoped(world["a"][0].id)
        if e.action.startswith("organization.lifecycle.")
    ]


def test_owner_requests_deletion_with_exact_name_and_grace_period(world):
    client = world["client"]
    wrong = client.post(REQUEST, headers=headers(world), json={"confirm_name": "org a"})
    assert wrong.status_code == 422
    ok = client.post(
        REQUEST, headers=headers(world), json={"confirm_name": "Org A", "reason": "bye"}
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["state"] == "deletion_requested" and body["reason"] == "bye"
    deadline = body["grace_deadline"]
    assert deadline and body["can_cancel_deletion"] is True
    again = client.post(REQUEST, headers=headers(world), json={"confirm_name": "Org A"})
    assert again.status_code == 403  # not on the pending-deletion allow-list
    with scope(world["a"]):
        repeated = world["runtime"].organization_lifecycle_service.request_deletion(
            confirm_name="Org A"
        )
    assert (
        str(repeated["grace_deadline"])
        and repeated["grace_deadline"].isoformat()[:19] == deadline[:19]
    )
    assert state(world) is State.DELETION_REQUESTED
    assert len(lifecycle_actions(world)) == 1
    row = client.get("/api/v0/account/me", headers=bearer(world)).json()["memberships"][
        0
    ]
    assert row["lifecycle_state"] == "deletion_requested"
    assert row["deletion_scheduled_for"] == deadline
    assert row["organization_status"] == "suspended"


def test_recovery_allow_list_is_narrow_and_owner_only(world):
    client = world["client"]
    client.post(REQUEST, headers=headers(world), json={"confirm_name": "Org A"})
    for path in (SETTINGS, "/api/v0/auth/me", "/api/v0/tasks", "/api/v0/users"):
        r = client.get(path, headers=headers(world))
        assert (r.status_code, r.json()) == (403, NOT_AVAILABLE), path
    assert client.get(LIFECYCLE, headers=headers(world)).status_code == 200
    for token, tenant in (("member_token", "a"), ("b_token", "a")):
        for method, path in (("GET", LIFECYCLE), ("POST", CANCEL), ("POST", EXPORT)):
            r = client.request(method, path, headers=headers(world, token, tenant))
            assert (r.status_code, r.json()) == (403, NOT_AVAILABLE)
    post = client.post(TRANSFER, headers=headers(world), json={})
    assert (post.status_code, post.json()) == (403, NOT_AVAILABLE)
    assert client.get(EXPORT, headers=headers(world)).status_code in (403, 405)


def test_owner_can_cancel_during_grace_and_work_resumes(world):
    client = world["client"]
    client.post(REQUEST, headers=headers(world), json={"confirm_name": "Org A"})
    ok = client.post(CANCEL, headers=headers(world))
    assert ok.status_code == 200 and ok.json()["state"] == "active"
    assert client.get(SETTINGS, headers=headers(world)).status_code == 200
    assert client.post(CANCEL, headers=headers(world)).status_code == 200
    assert sorted(e.action for e in lifecycle_actions(world)) == [
        "organization.lifecycle.deletion_cancelled",
        "organization.lifecycle.deletion_requested",
    ]


def test_cancel_is_refused_after_the_grace_period(world):
    client, runtime = world["client"], world["runtime"]
    client.post(REQUEST, headers=headers(world), json={"confirm_name": "Org A"})
    service = runtime.organization_lifecycle_service
    service.now = lambda: utc_now() + timedelta(days=8)
    assert client.post(CANCEL, headers=headers(world)).status_code == 409
    service.now = utc_now


def test_operator_requested_deletion_cannot_be_cancelled_by_the_owner(world, capsys):
    assert operator(world, capsys, "request-deletion")[0] == 0
    r = world["client"].post(CANCEL, headers=headers(world))
    assert r.status_code == 403 and "operator" in r.json()["detail"]
    assert operator(world, capsys, "cancel-deletion")[0] == 0
    assert state(world) is State.ACTIVE


def test_customer_admin_and_foreign_owner_cannot_use_owner_actions(world):
    client = world["client"]
    body = {
        "principal_id": world["member_a"][1].id,
        "expected_revision": 1,
        "confirmation": "Org A",
    }
    assert client.post(TRANSFER, headers=headers(world), json=body).status_code == 200
    assert client.get(LIFECYCLE, headers=headers(world)).status_code == 200
    for method, path, payload in (
        ("POST", REQUEST, {"confirm_name": "Org A"}),
        ("POST", CANCEL, None),
        ("POST", EXPORT, None),
    ):
        r = client.request(method, path, headers=headers(world), json=payload)
        assert r.status_code == 403, path
    assert state(world) is State.ACTIVE
    r = client.post(
        REQUEST, headers=headers(world, "b_token", "a"), json={"confirm_name": "Org A"}
    )
    assert r.status_code == 403


def test_non_account_credentials_cannot_request_deletion(world):
    runtime = world["runtime"]
    with scope(world["a"]):
        permissions = tuple(runtime.roles.list_scoped(world["a"][0].id)[0].permissions)
        _, _, _, token = runtime.credential_service.create_service_account(
            name="svc2", permissions=permissions
        )
    r = world["client"].post(
        REQUEST,
        headers={"Authorization": "Bearer " + token},
        json={"confirm_name": "Org A"},
    )
    assert r.status_code == 403 and state(world) is State.ACTIVE


def test_operator_suspension_is_operator_reversible_only(world, capsys):
    client = world["client"]
    status, out, logs = operator(world, capsys, "suspend")
    assert status == 0 and '"state": "suspended"' in out
    assert logs[-1]["event"] == "operator.organization_lifecycle"
    for method, path in (("GET", LIFECYCLE), ("POST", CANCEL), ("POST", EXPORT)):
        r = client.request(method, path, headers=headers(world))
        assert (r.status_code, r.json()) == (403, NOT_AVAILABLE)
    assert (
        client.get(SETTINGS, headers=headers(world, "b_token", "b")).status_code == 200
    )
    me = client.get("/api/v0/account/me", headers=bearer(world)).json()
    assert me["memberships"][0]["lifecycle_state"] == "suspended"
    assert operator(world, capsys, "suspend")[0] == 0
    assert operator(world, capsys, "request-deletion")[0] == 0
    assert state(world) is State.DELETION_REQUESTED
    assert operator(world, capsys, "resume")[0] == 2
    assert operator(world, capsys, "cancel-deletion")[0] == 0
    assert state(world) is State.SUSPENDED
    assert operator(world, capsys, "resume")[0] == 0
    assert client.get(SETTINGS, headers=headers(world)).status_code == 200
    events = lifecycle_actions(world)
    assert events
    assert {e.actor_principal_id for e in events} == {"operator:ops@example.test"}
    assert {e.metadata["actor_kind"] for e in events} == {"operator"}


def test_operator_actions_need_operator_reason_and_exact_confirmation(world, capsys):
    org = world["a"][0].id
    runtime = world["runtime"]
    with pytest.raises(SystemExit):
        organization_lifecycle.run(["suspend", "--organization-id", org], runtime)
    assert operator(world, capsys, "request-deletion")[0] == 0
    wrong = operator(world, capsys, "purge-now", "--confirm-organization-name", "nope")
    assert wrong[0] == 2 and state(world) is State.DELETION_REQUESTED
    right = operator(world, capsys, "purge-now", "--confirm-organization-name", "Org A")
    assert right[0] == 0 and state(world) is State.DELETING
    unknown = run_script(
        organization_lifecycle,
        capsys,
        runtime,
        "suspend",
        "--organization-id",
        "missing",
        *OP,
    )
    assert unknown[0] == 2


def test_purge_now_refused_unless_deletion_was_requested(world, capsys):
    result = operator(
        world, capsys, "purge-now", "--confirm-organization-name", "Org A"
    )
    assert result[0] == 2 and state(world) is State.ACTIVE


def test_transfer_is_refused_while_not_active(world, capsys):
    operator(world, capsys, "suspend")
    body = {
        "principal_id": world["member_a"][1].id,
        "expected_revision": 1,
        "confirmation": "Org A",
    }
    r = world["client"].post(TRANSFER, headers=headers(world), json=body)
    assert r.status_code == 403


def test_no_operator_action_is_reachable_over_http(world):
    routes = [r for r in world["client"].app.routes if hasattr(r, "path")]
    organization = {
        r.path for r in routes if r.path.startswith("/api/v0/organization/")
    }
    assert organization == {
        "/api/v0/organization/settings",
        "/api/v0/organization/limits",
        TRANSFER,
        "/api/v0/organization/members/{user_id}",
        LIFECYCLE,
        REQUEST,
        CANCEL,
        EXPORT,
    }
    for route in routes:
        assert not any(w in route.path for w in ("resume", "purge", "operator"))


def test_resume_does_not_replay_the_suspended_window(world, capsys):
    runtime = world["runtime"]
    org = world["a"][0].id
    with scope(world["a"]):
        schedule = runtime.workflow_schedules.list_scoped(org)[0]
    now = utc_now()
    runtime.workflow_schedules.put(
        schedule.model_copy(update={"next_run_at": now - timedelta(minutes=30)})
    )
    jobs_before = {j.job_id for j in runtime.execution_jobs.list_scoped(org)}
    operator(world, capsys, "suspend")
    assert runtime.dispatch_due_workflow_schedules(now=now) == ()
    assert runtime.workflow_schedules.get(schedule.schedule_id).next_run_at > now
    operator(world, capsys, "resume")
    assert runtime.dispatch_due_workflow_schedules(now=now) == ()
    assert {j.job_id for j in runtime.execution_jobs.list_scoped(org)} == jobs_before
    later = now + timedelta(seconds=120)
    runtime.dispatch_due_workflow_schedules(now=later)
    # Exactly one occurrence for the next interval; the 30-minute gap is not replayed.
    assert len(runtime.execution_jobs.list_scoped(org)) == len(jobs_before) + 1


@pytest.mark.parametrize("target", ["suspended", "deletion_requested", "deleting"])
def test_every_inactive_state_stops_work_and_selection(world, capsys, target):
    operator(world, capsys, "suspend" if target == "suspended" else "request-deletion")
    if target == "deleting":
        operator(world, capsys, "purge-now", "--confirm-organization-name", "Org A")
    assert state(world).value == target
    with scope(world["a"]), pytest.raises(PermissionError):
        world["runtime"].enqueue_agent_run("work")
    r = world["client"].get(SETTINGS, headers=headers(world))
    assert (r.status_code, r.json()) == (403, NOT_AVAILABLE)
