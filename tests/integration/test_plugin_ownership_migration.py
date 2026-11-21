"""Exercise classification, atomic activation and database ownership guards."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest
from psycopg.errors import CheckViolation, UndefinedFunction

from application.descriptor_catalog import scoped_descriptor, visible_descriptors
from infrastructure.database import plugin_ownership
from infrastructure.database.plugin_ownership import (
    activate_ownership,
    ownership_report,
)
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from tests.contracts.test_plugin_workflow_contracts import descriptor, installation
from tests.support.organizations import create_organization
from tests.support.runtime import LocalPlatformRuntime

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def catalog(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    runtime = LocalPlatformRuntime(postgres_store=store)
    runtime.bootstrap()
    other = create_organization(
        runtime,
        name="Other tenant",
        email="owner@other.test",
        display_name="Other owner",
    )
    try:
        yield runtime, store, other
    finally:
        runtime.close()


def _legacy(runtime, plugin_id, scopes):
    runtime.plugins.create(descriptor(plugin_id=plugin_id))
    for index, (org_id, workspace_id) in enumerate(scopes):
        runtime.installations.create(
            installation(
                plugin_installation_id=plugin_id + str(index),
                plugin_id=plugin_id,
                org_id=org_id,
                workspace_id=workspace_id,
            )
        )


def _activate(store):
    with store.connection() as connection:
        return activate_ownership(connection, reader_image=IMAGE)


def _payloads(store):
    with store.connection() as connection:
        return connection.execute(
            "SELECT collection,record_id,payload FROM product_records "
            "ORDER BY collection,record_id"
        ).fetchall()


def test_plan_backfill_preserves_payloads_and_classifies_without_guessing(catalog):
    runtime, store, other = catalog
    own, foreign = ("local-org", "local-workspace"), (other[0].id, other[1].id)
    _legacy(runtime, "mcp-owned", [own])
    _legacy(runtime, "mcp-orphan", [])
    _legacy(runtime, "mcp-conflict", [own, foreign])
    _legacy(runtime, "mcp-invalid", [(own[0], foreign[1])])
    runtime.plugins.create_owned_descriptor(
        descriptor(plugin_id="mcp-explicit"), own[0]
    )
    before = _payloads(store)
    with store.connection() as connection:
        plan = ownership_report(connection)
        assert not plan["applied"]
        assert (
            connection.execute(
                "SELECT max(version) FROM product_schema_migrations"
            ).fetchone()[0]
            == SCHEMA_HEAD_VERSION
        )
    report = _activate(store)
    assert report["applied"]
    assert report["reader_image"] == IMAGE
    entries = {entry["plugin_id"]: entry for entry in report["descriptors"]}
    assert entries["mcp-owned"]["org_id"] == own[0]
    assert entries["mcp-explicit"]["org_id"] == own[0]
    assert entries["mcp-orphan"]["reason"] == "orphaned_descriptor"
    assert entries["mcp-conflict"]["reason"] == "conflicting_organizations"
    assert entries["mcp-invalid"]["reason"] == "invalid_installation_scope"
    assert entries["github"]["state"] == "builtin"
    assert _payloads(store) == before
    assert _activate(store) == report
    assert scoped_descriptor(
        runtime.plugins, runtime.installations, "mcp-owned", own[0]
    )
    assert "mcp-owned" not in {
        p.plugin_id
        for p in visible_descriptors(runtime.plugins, runtime.installations, foreign[0])
    }


def test_guards_reject_owner_changes_and_foreign_installations(catalog):
    runtime, store, other = catalog
    _legacy(runtime, "mcp-owned", [("local-org", "local-workspace")])
    _activate(store)
    attempts = (
        ("UPDATE product_plugin_ownership_rollout SET reader_image=%s", ("changed",)),
        ("DELETE FROM product_plugin_ownership_rollout", ()),
        (
            "UPDATE product_records SET org_id=%s WHERE collection='plugins' "
            "AND record_id='mcp-owned'",
            (other[0].id,),
        ),
        (
            "UPDATE product_plugin_ownership SET org_id=%s WHERE plugin_id='mcp-owned'",
            (other[0].id,),
        ),
        ("DELETE FROM product_plugin_ownership WHERE plugin_id='mcp-owned'", ()),
        (
            "DELETE FROM product_records WHERE collection='plugins' "
            "AND record_id='mcp-owned'",
            (),
        ),
    )
    for query, parameters in attempts:
        with pytest.raises(CheckViolation), store.connection() as connection:
            connection.execute(query, parameters)
    with pytest.raises(CheckViolation):
        runtime.installations.create(
            installation(
                plugin_installation_id="forbidden-reference",
                plugin_id="mcp-owned",
                org_id=other[0].id,
                workspace_id=other[1].id,
            )
        )
    assert runtime.plugins.get("mcp-owned").display_name == "Custom MCP"


def test_new_writer_and_reopened_readers_keep_explicit_scope(
    catalog, isolated_postgres_url
):
    runtime, store, other = catalog
    _activate(store)
    created = descriptor(plugin_id="mcp-new-owned")
    runtime.plugins.create_owned_descriptor(created, "local-org")
    runtime.plugins.put(
        created.model_copy(update={"display_name": "Updated connector"})
    )
    runtime.installations.create(
        installation(
            plugin_installation_id="new-owned-reference",
            plugin_id=created.plugin_id,
            org_id="local-org",
            workspace_id="local-workspace",
        )
    )
    runtime.plugins.create(descriptor(plugin_id="mcp-new-unowned"))
    assert runtime.plugins.ownership_state("mcp-new-unowned") == "quarantined"
    with pytest.raises(CheckViolation):
        runtime.installations.create(
            installation(
                plugin_installation_id="unowned-reference",
                plugin_id="mcp-new-unowned",
                org_id="local-org",
                workspace_id="local-workspace",
            )
        )
    reopened = LocalPlatformRuntime(
        postgres_store=ProductPostgresStore(isolated_postgres_url)
    )
    try:
        assert scoped_descriptor(
            reopened.plugins, reopened.installations, created.plugin_id, "local-org"
        )
        with pytest.raises(KeyError):
            scoped_descriptor(
                reopened.plugins, reopened.installations, created.plugin_id, other[0].id
            )
        with store.connection() as connection:
            org_id, payload = connection.execute(
                "SELECT org_id,payload FROM product_records WHERE collection='plugins' "
                "AND record_id=%s",
                (created.plugin_id,),
            ).fetchone()
        assert org_id == "local-org" and "org_id" not in payload
    finally:
        reopened.close()
    runtime.integration_admin_service.disconnect_plugin_installation(
        "new-owned-reference"
    )
    with pytest.raises(KeyError):
        runtime.plugins.get(created.plugin_id)
    assert runtime.plugins.ownership_state(created.plugin_id) is None


def test_startup_is_reader_only_and_rejects_partial_activation(
    catalog, isolated_postgres_url
):
    _, store, _ = catalog
    with store.connection() as connection:
        assert not ownership_report(connection)["applied"]
        connection.execute("INSERT INTO product_schema_migrations(version) VALUES (20)")
    with pytest.raises(RuntimeError, match=r"missing.*tables"):
        ProductPostgresStore(isolated_postgres_url)


def test_reopened_reader_refuses_disabled_ownership_guard(
    catalog, isolated_postgres_url
):
    _, store, _ = catalog
    _activate(store)
    with store.connection() as connection:
        connection.execute(
            "ALTER TABLE product_records DISABLE TRIGGER "
            "plugin_installation_owner_guard"
        )
    with pytest.raises(RuntimeError, match="missing guards"):
        ProductPostgresStore(isolated_postgres_url)


def test_owned_catalog_reads_do_not_load_global_repositories(catalog, monkeypatch):
    runtime, store, _ = catalog
    _legacy(runtime, "mcp-owned", [("local-org", "local-workspace")])
    _activate(store)

    def forbidden():
        raise AssertionError("Scoped catalog used an unscoped repository list")

    monkeypatch.setattr(runtime.plugins, "list", forbidden)
    monkeypatch.setattr(runtime.installations, "list", forbidden)
    assert scoped_descriptor(
        runtime.plugins, runtime.installations, "mcp-owned", "local-org"
    )
    assert visible_descriptors(runtime.plugins, runtime.installations, "local-org")


def test_operator_default_is_read_only(
    catalog, isolated_postgres_url, monkeypatch, capsys
):
    import json

    from apps.cli.plugin_catalog_ownership import main

    _, store, _ = catalog
    before = _payloads(store)
    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setattr("sys.argv", ["plugin_catalog_ownership"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert not result["applied"]
    assert _payloads(store) == before


def test_quarantine_cannot_disappear_when_conflicting_references_are_removed(catalog):
    runtime, store, other = catalog
    _legacy(
        runtime,
        "mcp-ambiguous",
        [("local-org", "local-workspace"), (other[0].id, other[1].id)],
    )
    _activate(store)
    runtime.integration_admin_service.disconnect_plugin_installation("mcp-ambiguous0")
    with pytest.raises(KeyError):
        scoped_descriptor(
            runtime.plugins, runtime.installations, "mcp-ambiguous", other[0].id
        )
    with store.connection() as connection:
        before = ownership_report(connection)
    assert _activate(store) == before
    with pytest.raises(CheckViolation), store.connection() as connection:
        connection.execute(
            "DELETE FROM product_records WHERE collection='plugins' "
            "AND record_id='mcp-ambiguous'"
        )


def test_failed_activation_rolls_back_and_is_retryable(catalog, monkeypatch):
    _runtime, store, _ = catalog
    before = _payloads(store)
    original = plugin_ownership.GUARDS
    monkeypatch.setattr(
        plugin_ownership, "GUARDS", (*original, "SELECT missing_migration_failure()")
    )
    with pytest.raises(UndefinedFunction):
        _activate(store)
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT to_regclass('product_plugin_ownership')"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT max(version) FROM product_schema_migrations"
            ).fetchone()[0]
            == SCHEMA_HEAD_VERSION
        )
    assert _payloads(store) == before
    monkeypatch.setattr(plugin_ownership, "GUARDS", original)
    assert _activate(store)["applied"]


def test_concurrent_activations_do_not_duplicate_or_reclassify(
    catalog, isolated_postgres_url
):
    def activate(_):
        with psycopg.connect(isolated_postgres_url) as connection:
            return activate_ownership(connection, reader_image=IMAGE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(activate, range(2)))
    assert first == second


def test_activation_requires_bounded_records_and_digest(catalog):
    _, store, _ = catalog
    with pytest.raises(ValueError), store.connection() as connection:
        activate_ownership(connection, reader_image="mutable:latest")
    with pytest.raises(RuntimeError, match="limit"), store.connection() as connection:
        activate_ownership(connection, reader_image=IMAGE, max_records=1)
    with store.connection() as connection:
        assert not ownership_report(connection)["applied"]


def test_new_reference_serializes_against_descriptor_deletion(
    catalog, isolated_postgres_url
):
    from psycopg.types.json import Jsonb

    runtime, store, _ = catalog
    _activate(store)
    runtime.plugins.create_owned_descriptor(
        descriptor(plugin_id="mcp-race"), "local-org"
    )
    record = installation(
        plugin_installation_id="racing-installation",
        plugin_id="mcp-race",
        org_id="local-org",
        workspace_id="local-workspace",
    )
    ready, pid = Event(), []

    def delete():
        try:
            with psycopg.connect(isolated_postgres_url) as connection:
                pid.append(connection.info.backend_pid)
                ready.set()
                connection.execute("SET statement_timeout='10s'")
                connection.execute(
                    "DELETE FROM product_records WHERE collection='plugins' "
                    "AND record_id='mcp-race'"
                )
            return "deleted"
        except CheckViolation:
            return "retained"

    with psycopg.connect(isolated_postgres_url) as writer:
        writer.execute(
            "INSERT INTO product_records"
            "(collection,record_id,org_id,workspace_id,payload) "
            "VALUES ('plugin_installations',%s,%s,%s,%s)",
            (
                record.plugin_installation_id,
                record.org_id,
                record.workspace_id,
                Jsonb(record.model_dump(mode="json")),
            ),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delete)
            try:
                assert ready.wait(5)
                assert _waiting_for_lock(store, pid[0], future)
            finally:
                writer.commit()
            assert future.result(timeout=5) == "retained"
    assert runtime.plugins.get("mcp-race")
    assert runtime.installations.get("racing-installation")


def _waiting_for_lock(store, pid, future):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with store.connection() as connection:
            row = connection.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()
        if row and row[0] == "Lock":
            return True
        if future.done():
            return False
        time.sleep(0.01)
    return False


def test_reader_cannot_mix_legacy_query_with_post_activation_data(
    catalog, isolated_postgres_url, monkeypatch
):
    runtime, store, _ = catalog
    with store.connection() as connection:
        connection.execute(
            "UPDATE product_records SET org_id='local-org' "
            "WHERE collection='plugins' AND record_id='github'"
        )
    catalog_repo = runtime.plugins
    original = catalog_repo._visible_query
    reader_paused, release_reader, activation_ready = Event(), Event(), Event()
    activation_pid = []

    def pause_after_version_check(connection, org_id):
        result = original(connection, org_id)
        reader_paused.set()
        assert release_reader.wait(8)
        return result

    def activate():
        with psycopg.connect(isolated_postgres_url) as connection:
            activation_pid.append(connection.info.backend_pid)
            activation_ready.set()
            return activate_ownership(connection, reader_image=IMAGE)

    monkeypatch.setattr(catalog_repo, "_visible_query", pause_after_version_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(catalog_repo.visible_descriptors, "local-org")
        assert reader_paused.wait(5)
        migrating = pool.submit(activate)
        try:
            assert activation_ready.wait(5)
            assert _waiting_for_lock(store, activation_pid[0], migrating)
        finally:
            release_reader.set()
        assert "github" not in {item.plugin_id for item in reading.result(timeout=5)}
        assert migrating.result(timeout=5)["applied"]
    monkeypatch.setattr(catalog_repo, "_visible_query", original)
    assert "github" not in {
        item.plugin_id for item in catalog_repo.visible_descriptors("local-org")
    }
