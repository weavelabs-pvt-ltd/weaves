"""Slice 6.4 on PostgreSQL: row security, switch latency, quarantine and audit."""

from __future__ import annotations

import json
import time
from contextlib import ExitStack

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import (
    ApiCredentialStatus,
    WorkflowScheduleStatus,
    WorkflowTriggerStatus,
)
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.auth.google_account_resolution import (
    GoogleAccountResolver,
    SignInRefused,
)
from modules.auth.google_oidc import GoogleClaims
from modules.recovery.tombstones import FileTombstoneLog
from shared.request_context import require_tenant_context, system_scope
from tests.support.lifecycle_http import CANCEL, populated_world
from tests.support.lifecycle_world import rows
from tests.support.operator_scripts import (
    organization_lifecycle,
    organization_limits,
    platform_control,
    run_script,
)
from tests.support.organization_administration import headers, scope

OP = ("--operator", "ops@example.test", "--reason", "pg test")
SETTINGS = "/api/v0/organization/settings"


def open_runtime(url, role):
    pool = open_postgres_pool(url, min_size=1, max_size=6)
    store = ProductPostgresStore(pool=pool, app_role=role)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


@pytest.fixture
def world(isolated_postgres_url, monkeypatch, tmp_path):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_TOMBSTONE_DIR", str(tmp_path / "tombstones"))
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
            yield world
    finally:
        require_tenant_context(False)
        for runtime in (api, worker, operator):
            runtime.close()
        owner.close()


def sql(world, query, *params):
    with psycopg.connect(world["url"], autocommit=True) as connection:
        return connection.execute(query, params).fetchall()


def control(world, capsys, *args):
    return run_script(platform_control, capsys, world["operator"], *args)


def quarantine(world, capsys, command, org="a", *extra):
    return run_script(
        organization_lifecycle,
        capsys,
        world["operator"],
        command,
        "--organization-id",
        world[org][0].id,
        *OP,
        *extra,
    )


def actions(world):
    return sql(
        world,
        "SELECT command, target, result, operator, reason, correlation_id, org_id "
        "FROM operator_actions ORDER BY at, action_id",
    )


# ---- row security and append-only audit --------------------------------


def as_app(url):
    connection = psycopg.connect(url, autocommit=True)
    connection.execute("SET ROLE weaves_app")
    return connection


def test_restricted_role_reads_switches_but_cannot_write_or_see_audit(world, capsys):
    control(world, capsys, "set", *OP, "--worker-paused", "true")
    assert actions(world)  # the owner wrote an audit row
    with as_app(world["url"]) as app:
        assert app.execute("SELECT count(*) FROM platform_controls").fetchone()[0] == 1
        updated = app.execute("UPDATE platform_controls SET read_only = true")
        assert updated.rowcount == 0
        assert app.execute("SELECT count(*) FROM operator_actions").fetchone()[0] == 0
        assert app.execute("SELECT count(*) FROM org_quarantines").fetchone()[0] == 0
        with pytest.raises(psycopg.Error):
            app.execute(
                "INSERT INTO operator_actions (action_id, operator, command, reason, "
                "correlation_id, result) VALUES ('x', 'o', 'c', 'r', 'cid', 'ok')"
            )
        with pytest.raises(psycopg.Error):
            app.execute(
                "INSERT INTO org_quarantines (org_id, state, quarantined_by, reason, "
                "correlation_id, quarantined_at) "
                "VALUES ('x', 'quarantined', 'o', 'r', 'c', now())"
            )
    stored = sql(world, "SELECT read_only, worker_paused FROM platform_controls")
    assert stored == [(None, True)]


def test_operator_actions_is_append_only_even_for_the_owner(world, capsys):
    control(world, capsys, "set", *OP, "--read-only", "true")
    with psycopg.connect(world["url"], autocommit=True) as owner:
        for statement in (
            "UPDATE operator_actions SET reason = 'edited'",
            "DELETE FROM operator_actions",
            "TRUNCATE operator_actions",
        ):
            with pytest.raises(psycopg.Error, match="append-only"):
                owner.execute(statement)
    assert len(actions(world)) == 1


def test_platform_controls_holds_exactly_one_row(world):
    with (
        psycopg.connect(world["url"], autocommit=True) as owner,
        pytest.raises(psycopg.Error),
    ):
        owner.execute("INSERT INTO platform_controls (id) VALUES (2)")


# ---- switches take effect without a restart ------------------------------


def wait_until(check, limit=7.0):
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if check():
            return time.monotonic() - started
        time.sleep(0.1)
    raise AssertionError("switch did not take effect in time")


def test_switch_latency_is_bounded_across_processes_without_restart(world, capsys):
    worker_controls = world["worker"].platform_controls
    assert worker_controls.snapshot().worker_paused is False  # primed, cached
    status, out, _ = control(world, capsys, "set", *OP, "--worker-paused", "true")
    assert status == 0 and json.loads(out)["overrides"]["worker_paused"] is True
    waited = wait_until(lambda: worker_controls.snapshot().worker_paused)
    assert waited <= 6.0  # the 5 s cache, no restart
    control(world, capsys, "clear", *OP, "--switch", "worker_paused")
    wait_until(lambda: not worker_controls.snapshot().worker_paused)


def test_read_only_refuses_http_writes_then_recovers(world, capsys):
    client = world["client"]
    assert client.post(CANCEL, headers=headers(world)).status_code == 200
    control(
        world,
        capsys,
        "set",
        *OP,
        "--read-only",
        "true",
        "--message",
        "Restoring a backup.",
    )

    def refused():
        response = client.post(CANCEL, headers=headers(world))
        return response.status_code == 503

    assert wait_until(refused) <= 6.0
    body = client.post(CANCEL, headers=headers(world)).json()
    assert body == {"detail": "Restoring a backup.", "code": "platform_read_only"}
    assert client.get(SETTINGS, headers=headers(world)).status_code == 200
    control(world, capsys, "clear", *OP)
    wait_until(lambda: client.post(CANCEL, headers=headers(world)).status_code == 200)


def test_admissions_pause_refuses_new_work_for_everyone_then_clears(world, capsys):
    control(world, capsys, "set", *OP, "--admissions-paused", "true")
    admission = world["api"].admission
    world["api"].platform_controls.forget()
    for tenant in ("a", "b"):
        with scope(world[tenant]), pytest.raises(Exception) as caught:
            world["api"].enqueue_agent_run("while paused")
        assert getattr(caught.value, "resource", "") == "admissions_paused"
    assert admission.usage(world["a"][0].id).admissions_paused is True
    control(world, capsys, "clear", *OP)
    world["api"].platform_controls.forget()
    with scope(world["a"]):
        world["api"].enqueue_agent_run("after clear")


def test_signups_can_be_closed_without_locking_out_existing_accounts(world, capsys):
    service = world["api"].account_sign_in_service
    resolver = GoogleAccountResolver(
        service, signups_open=world["api"].platform_controls.signups_open
    )
    existing = GoogleClaims("iss", "sub-owner", "owner-a@example.test", "Owner A")
    brand_new = GoogleClaims("iss", "sub-new", "new.person@example.test", "New")
    control(world, capsys, "set", *OP, "--signup-enabled", "false")
    world["api"].platform_controls.forget()
    with system_scope("account"):
        assert resolver.resolve(existing).id
        with pytest.raises(SignInRefused) as caught:
            resolver.resolve(brand_new)
        assert caught.value.code == "signup_closed"
    control(world, capsys, "clear", *OP)
    world["api"].platform_controls.forget()
    with system_scope("account"):
        assert resolver.resolve(brand_new).id


# ---- quarantine -----------------------------------------------------------


def tenant_state(world, key):
    runtime, org = world["operator"], world[key][0].id
    return {
        "schedules": [s.status for s in runtime.workflow_schedules.list_scoped(org)],
        "triggers": [t.status for t in runtime.workflow_triggers.list_scoped(org)],
        "credentials": [c.status for c in runtime.api_credentials.list_scoped(org)],
        "org": runtime.organizations.get(org).status,
    }


def test_quarantine_blocks_a_only_and_release_restores_exactly_that(world, capsys):
    client, b_before = world["client"], rows(world["operator"], world["b"][0].id)
    token = world["seed_a"]["secrets"][3]
    with system_scope("auth"):
        principal = world["api"].authentication_service.authenticate_api_token(token)
    assert principal
    assert tenant_state(world, "a")["schedules"] == [WorkflowScheduleStatus.ACTIVE]
    assert tenant_state(world, "a")["triggers"] == [WorkflowTriggerStatus.ACTIVE]

    status, out, logs = quarantine(world, capsys, "quarantine")
    assert status == 0, out
    result = json.loads(out)
    assert result["state"] == "suspended"
    assert result["quarantine"]["schedules_disabled"] == 1
    assert result["quarantine"]["triggers_disabled"] == 1
    assert result["quarantine"]["credentials_revoked"] == 0
    cid = result["quarantine"]["correlation_id"]
    assert any(line.get("correlation_id") == cid for line in logs)

    # A is stopped everywhere; the stop is immediate (no cache on lifecycle).
    assert client.get(SETTINGS, headers=headers(world)).status_code == 403
    assert client.post(CANCEL, headers=headers(world)).status_code == 403
    with system_scope("auth"), pytest.raises(PermissionError):
        world["api"].organization_service.resolve_principal_scope(principal)
    state = tenant_state(world, "a")
    assert state["schedules"] == [WorkflowScheduleStatus.DISABLED]
    assert state["triggers"] == [WorkflowTriggerStatus.DISABLED]
    assert state["credentials"] == [ApiCredentialStatus.ACTIVE] * len(
        state["credentials"]
    )

    # B is completely unaffected: still serving, rows byte-identical.
    b_headers = headers(world, "b_token", "b")
    assert client.get(SETTINGS, headers=b_headers).status_code == 200
    assert rows(world["operator"], world["b"][0].id) == b_before
    assert tenant_state(world, "b")["schedules"] == [WorkflowScheduleStatus.ACTIVE]

    # Repeating is idempotent and records another audit row.
    assert quarantine(world, capsys, "quarantine")[0] == 0

    status, out, _ = quarantine(world, capsys, "release-quarantine")
    assert status == 0, out
    assert json.loads(out)["state"] == "active"
    assert client.get(SETTINGS, headers=headers(world)).status_code == 200
    state = tenant_state(world, "a")
    assert state["schedules"] == [WorkflowScheduleStatus.ACTIVE]
    assert state["triggers"] == [WorkflowTriggerStatus.ACTIVE]
    assert rows(world["operator"], world["b"][0].id) == b_before

    names = [row[0] for row in actions(world)]
    assert names.count("organization.quarantine") == 2
    assert names.count("organization.release_quarantine") == 1
    for row in actions(world):
        assert row[3] == "ops@example.test" and row[4] == "pg test"
        assert row[5].startswith("op-") and row[6] == world["a"][0].id
    # The release row shares nothing it should not: B has no audit rows at all.
    assert not [r for r in actions(world) if r[6] == world["b"][0].id]


def test_release_does_not_resume_an_organization_suspended_by_someone_else(
    world, capsys
):
    assert quarantine(world, capsys, "suspend")[0] == 0
    assert quarantine(world, capsys, "quarantine")[0] == 0
    assert quarantine(world, capsys, "release-quarantine")[0] == 0
    assert world["operator"].organizations.get(world["a"][0].id).status.value == (
        "suspended"
    )
    assert quarantine(world, capsys, "resume")[0] == 0
    assert world["client"].get(SETTINGS, headers=headers(world)).status_code == 200


def test_release_leaves_things_changed_after_quarantine_alone(world, capsys):
    assert quarantine(world, capsys, "quarantine")[0] == 0
    org = world["a"][0].id
    runtime = world["operator"]
    schedule = runtime.workflow_schedules.list_scoped(org)[0]
    runtime.workflow_schedules.put(
        schedule.model_copy(update={"status": WorkflowScheduleStatus.ACTIVE})
    )
    before = runtime.workflow_schedules.list_scoped(org)[0].updated_at
    assert quarantine(world, capsys, "release-quarantine")[0] == 0
    assert runtime.workflow_schedules.list_scoped(org)[0].updated_at == before


def test_revoking_credentials_is_irreversible_and_tombstoned(world, capsys, tmp_path):
    status, out, _ = quarantine(
        world, capsys, "quarantine", "a", "--revoke-credentials"
    )
    assert status == 0, out
    assert json.loads(out)["quarantine"]["credentials_revoked"] >= 1
    revoked = [
        c
        for c in world["operator"].api_credentials.list_scoped(world["a"][0].id)
        if c.status is ApiCredentialStatus.REVOKED
    ]
    assert revoked
    logged = FileTombstoneLog(str(tmp_path / "tombstones")).read()
    assert {t.subject_id for t in logged if t.kind == "credential_revoked"} >= {
        c.id for c in revoked
    }
    assert quarantine(world, capsys, "release-quarantine")[0] == 0
    still = world["operator"].api_credentials.list_scoped(world["a"][0].id)
    assert all(c.status is ApiCredentialStatus.REVOKED for c in revoked if c in still)
    assert {c.id for c in still if c.status is ApiCredentialStatus.REVOKED} >= {
        c.id for c in revoked
    }
    # B's credentials were never touched.
    assert all(
        c.status is ApiCredentialStatus.ACTIVE
        for c in world["operator"].api_credentials.list_scoped(world["b"][0].id)
    )


def test_refused_attempts_are_audited_too(world, capsys):
    status, _, logs = quarantine(world, capsys, "release-quarantine")
    assert status == 2  # not quarantined
    row = actions(world)[-1]
    assert (row[0], row[2]) == ("organization.release-quarantine", "refused")
    assert any(line["event"].endswith(".refused") for line in logs)
    status, _, _ = quarantine(world, capsys, "request-deletion")
    assert status == 0
    assert quarantine(world, capsys, "quarantine")[0] == 2  # deletion pending


def test_quarantine_status_is_read_only_and_unaudited(world, capsys):
    before = len(actions(world))
    status, out, _ = run_script(
        organization_lifecycle,
        capsys,
        world["operator"],
        "quarantine-status",
        "--organization-id",
        world["a"][0].id,
    )
    assert status == 0 and json.loads(out)["quarantine"] is None
    assert len(actions(world)) == before


def test_every_lifecycle_and_limits_command_writes_an_audit_row(world, capsys):
    org = world["a"][0].id
    for command in ("suspend", "resume"):
        assert quarantine(world, capsys, command)[0] == 0
    status, _, _ = run_script(
        organization_limits,
        capsys,
        world["operator"],
        "set",
        "--organization-id",
        org,
        *OP,
        "--seats",
        "60",
    )
    assert status == 0
    status, _, _ = run_script(
        organization_limits,
        capsys,
        world["operator"],
        "clear",
        "--organization-id",
        org,
        *OP,
    )
    assert status == 0
    commands = [row[0] for row in actions(world)]
    assert commands == [
        "organization.suspend",
        "organization.resume",
        "organization.limits.set",
        "organization.limits.clear",
    ]
    limits_row = sql(
        world,
        "SELECT details FROM operator_actions "
        "WHERE command = 'organization.limits.set'",
    )[0][0]
    assert limits_row == {"limits": {"seats": 60}}


def test_audit_rows_never_contain_secrets(world, capsys):
    quarantine(world, capsys, "quarantine", "a", "--revoke-credentials")
    dump = json.dumps(sql(world, "SELECT * FROM operator_actions"), default=str)
    for secret in world["seed_a"]["secrets"]:
        assert secret not in dump
