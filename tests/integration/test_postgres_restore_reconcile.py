"""Restore an old dump, reconcile: deleted tenants and revocations stay applied."""

import importlib
import io
import json
import sys
from pathlib import Path

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import ApiCredentialStatus
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.recovery.restore_gate import (
    RestoreNotReconciled,
    assert_services_may_start,
)
from modules.recovery.tombstones import FileTombstoneLog
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
)
from tests.support.lifecycle_http import drain, populated_world
from tests.support.lifecycle_world import rows

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra/gcp/singlevm"))
restore_reconcile = importlib.import_module("restore_reconcile")


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
    worker, operator = (
        open_runtime(isolated_postgres_url, role) for role in ("weaves_app", "")
    )
    require_tenant_context(True)
    world = populated_world(worker)
    world.update(
        worker=worker,
        operator=operator,
        url=isolated_postgres_url,
        dir=tmp_path / "tombstones",
        log=FileTombstoneLog(tmp_path / "tombstones"),
    )
    try:
        yield world
    finally:
        require_tenant_context(False)
        worker.close()
        operator.close()


def tables(world):
    # operator_actions is append-only (it refuses TRUNCATE by design); the real
    # restore loads a dump into an empty database rather than truncating.
    with psycopg.connect(world["url"]) as connection:
        found = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
            "AND tablename NOT IN ('product_schema_migrations', 'operator_actions') "
            "ORDER BY tablename"
        ).fetchall()
    return [name for (name,) in found]


def dump(world):
    """Stand-in for pg_dump: every table's rows as COPY data."""
    out = {}
    with psycopg.connect(world["url"]) as connection:
        for name in tables(world):
            buffer = io.BytesIO()
            with connection.cursor().copy(f"COPY {name} TO STDOUT") as copy:
                for chunk in copy:
                    buffer.write(bytes(chunk))
            out[name] = buffer.getvalue()
    return out


def restore(world, snapshot):
    with psycopg.connect(world["url"]) as connection:
        names = tables(world)
        connection.execute("TRUNCATE " + ", ".join(names) + " CASCADE")
        for name in names:
            with connection.cursor().copy(f"COPY {name} FROM STDIN") as copy:
                copy.write(snapshot[name])


def credential_of(world, org):
    org_id, seed = world[org][0].id, world["seed_" + org]
    token = set_request_identity("p", org_id, world[org][1].id)
    try:
        return next(
            c
            for c in world["worker"].api_credentials.list_scoped(org_id)
            if c.token_digest in seed["secrets"]
        )
    finally:
        reset_request_principal(token)


def secret_ref_of(world, org):
    return next(v for v in world["seed_" + org]["secrets"] if v.startswith("secret:"))


def status_of(world, org_id):
    token = set_request_identity("p", org_id, "x")
    try:
        return world["operator"].organization_lifecycle_service.state_of(org_id).value
    finally:
        reset_request_principal(token)


def apply(world, capsys, *extra):
    capsys.readouterr()
    code = restore_reconcile.run(
        ["--dir", str(world["dir"]), "apply", *extra], world["operator"]
    )
    return code, json.loads(capsys.readouterr().out)


def fingerprint(world):
    return {name: sorted(rows_of(world, name)) for name in tables(world)}


def rows_of(world, name):
    with psycopg.connect(world["url"]) as connection:
        return [
            str(r)
            for r in connection.execute(f"SELECT t::text FROM {name} t").fetchall()
        ]


def test_restore_old_dump_then_reconcile(world, capsys):
    a, b = world["a"][0].id, world["b"][0].id
    head = world["log"].head()
    snapshot = dump(world)
    b_before = rows(world["operator"], b)
    credential_b = credential_of(world, "b")
    ref_b = secret_ref_of(world, "b")

    # After the backup: delete org A for good, revoke B's credential, delete B's secret.
    op = world["operator"].organization_lifecycle_service
    op.operator_request_deletion(a, "ops@example.test", "test")
    op.purge_now(a, "ops@example.test", "test", "Org A")
    drain(world["worker"])
    assert status_of(world, a) == "deleted"
    token = set_request_identity(world["b"][3].id, b, world["b"][1].id)
    try:
        world["worker"].revoke_api_credential(credential_b.id)
        world["worker"].secret_store.delete(ref_b)
    finally:
        reset_request_principal(token)
    kinds = [t.kind for t in world["log"].read()]
    assert {"organization_purge_started", "organization_deleted"} <= set(kinds)
    assert {"credential_revoked", "secret_deleted"} <= set(kinds)

    # Restore the old dump: the tenant and credential come back to life.
    restore(world, snapshot)
    assert status_of(world, a) == "active"
    assert credential_of(world, "b").status is ApiCredentialStatus.ACTIVE
    head_file = world["dir"] / "head.json"
    head_file.write_text(json.dumps(head))
    assert (
        restore_reconcile.run(
            ["--dir", str(world["dir"]), "mark-restore", "--backup", "x.sql.gz"]
        )
        == 0
    )
    capsys.readouterr()
    with pytest.raises(RestoreNotReconciled):
        assert_services_may_start(world["dir"])

    code, dry = apply(world, capsys, "--head-file", str(head_file), "--dry-run")
    assert code == 0 and dry["dry_run"] and status_of(world, a) == "active"

    code, report = apply(world, capsys, "--head-file", str(head_file))
    assert code == 0
    assert report["organizations"] == {a: "deleted"}
    assert report["credentials"] == {credential_b.id: "revoked"}
    assert report["secrets"] == {ref_b: "deleted"}
    assert status_of(world, a) == "deleted"
    token = set_request_identity("p", a, "x")
    try:
        assert world["operator"].organization_purge_service.remaining_rows(a) == 0
    finally:
        reset_request_principal(token)
    assert credential_of(world, "b").status is ApiCredentialStatus.REVOKED
    assert_services_may_start(world["dir"])  # gate cleared

    # Nothing else touched: org B equals its pre-backup rows except the credential.
    b_after = rows(world["operator"], b)
    assert b_after.pop("api_credentials") != b_before.pop("api_credentials")
    assert b_after == b_before
    with psycopg.connect(world["url"]) as connection:
        assert connection.execute(
            "SELECT count(*) FROM product_secrets WHERE secret_ref = %s", (ref_b,)
        ).fetchone() == (0,)

    # Idempotent: a second run changes nothing and reports the same outcomes.
    before = fingerprint(world)
    code, again = apply(
        world, capsys, "--head-file", str(head_file), "--restore-id", report_id(world)
    )
    assert code == 0
    assert again["organizations"] == {a: "deleted"}
    assert again["credentials"] == {credential_b.id: "already_revoked"}
    after = fingerprint(world)
    assert {k: v for k, v in after.items() if k != "account_sessions"} == {
        k: v for k, v in before.items() if k != "account_sessions"
    }


def report_id(world):
    return next(world["dir"].glob("restore-reconciled-*.json")).name[
        len("restore-reconciled-") : -len(".json")
    ]


def test_apply_refuses_when_log_is_shorter_than_the_backup(world, capsys):
    snapshot = dump(world)
    world["log"].record("credential_revoked", "x", subject_id="c1")
    head = world["log"].head()
    (world["dir"] / "tombstones.jsonl").write_text("")  # log lost
    restore(world, snapshot)
    head_file = world["dir"] / "head.json"
    head_file.write_text(json.dumps(head))
    restore_reconcile.run(["--dir", str(world["dir"]), "mark-restore", "--backup", "x"])
    capsys.readouterr()
    code, _ = apply_raw(world, capsys, "--head-file", str(head_file))
    assert code == 2
    with pytest.raises(RestoreNotReconciled):
        assert_services_may_start(world["dir"])


def test_apply_requires_head_or_explicit_override(world, capsys):
    restore_reconcile.run(["--dir", str(world["dir"]), "mark-restore", "--backup", "x"])
    capsys.readouterr()
    code, _ = apply_raw(world, capsys)
    assert code == 2


def apply_raw(world, capsys, *extra):
    capsys.readouterr()
    code = restore_reconcile.run(
        ["--dir", str(world["dir"]), "apply", *extra], world["operator"]
    )
    return code, capsys.readouterr().out


def test_unwritable_log_fails_purge_closed_without_erasing(world, monkeypatch):
    a = world["a"][0].id
    op = world["operator"].organization_lifecycle_service
    op.operator_request_deletion(a, "ops@example.test", "test")
    op.purge_now(a, "ops@example.test", "test", "Org A")
    before = rows(world["operator"], a)
    monkeypatch.setenv("WEAVES_TOMBSTONE_DIR", "/proc/forbidden/tombstones")
    purge = world["worker"].organization_purge_service
    from modules.recovery.tombstones import tombstones_from_env

    purge.tombstones = tombstones_from_env()
    assert purge.tick() == 0  # failure is logged per organization, not raised
    assert rows(world["operator"], a) == before
    assert status_of(world, a) == "deleting"
