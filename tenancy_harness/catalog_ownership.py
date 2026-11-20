"""Rehearse explicit ownership activation against the warm native API."""

import json
import os
from pathlib import Path

from psycopg.errors import CheckViolation

from apps.runtime_wiring import open_process_runtime
from infrastructure.database.plugin_ownership import activate_ownership
from infrastructure.database.store import ProductPostgresStore
from shared.request_context import enter_system_scope, exit_system_scope
from tenancy_harness.client import PROVIDER, call
from tenancy_harness.seed import require_disposable_database


def migrate_catalog(fixtures):
    """Create legacy ambiguity, activate atomically, then use the new API writer."""
    require_disposable_database()
    a, b = fixtures["tenants"].values()
    runtime = open_process_runtime(process="cli")
    operator = enter_system_scope("bootstrap")
    try:
        original = runtime.plugins.get(a["installation"]["plugin_id"])
        conflict_id, orphan_id = "mcp-migration-conflict", "mcp-migration-orphan"
        for plugin_id in (conflict_id, orphan_id):
            runtime.plugins.create(
                original.model_copy(
                    update={
                        "plugin_id": plugin_id,
                        "display_name": "MIGRATION_QUARANTINE_SENTINEL",
                    }
                )
            )
        original_installation = runtime.installations.get(
            a["installation"]["plugin_installation_id"]
        )
        ids = {}
        for tenant in (a, b):
            installation_id = "migration-conflict-" + tenant["label"]
            ids[tenant["label"]] = installation_id
            runtime.installations.create(
                original_installation.model_copy(
                    update={
                        "plugin_installation_id": installation_id,
                        "plugin_id": conflict_id,
                        "org_id": tenant["org_id"],
                        "workspace_id": tenant["workspace_id"],
                        "display_name": "Legacy quarantine fixture",
                        "auth_ref": None,
                    }
                )
            )
    finally:
        exit_system_scope(operator)
        runtime.close()
    legacy_scoped = _legacy_catalog_is_scoped(fixtures)
    store = ProductPostgresStore(os.environ["DATABASE_URL"], app_role="")
    try:
        with store.connection() as connection:
            report = activate_ownership(
                connection, reader_image=os.environ["TENANCY_CATALOG_READER_IMAGE"]
            )
    finally:
        store.close()
    denied = _foreign_reference_is_denied(a, b)
    created = call(
        "POST",
        "/plugin-installations",
        a["admin"],
        {
            "plugin_id": "mcp",
            "display_name": a["marker"] + " new owned",
            "endpoint_url": PROVIDER + "/mcp",
            "bearer_token": a["marker"],
            "read_only_tool_names": ["search_docs"],
            "catalog_connector_id": "custom-mcp",
        },
    )
    return {
        "version": 20,
        "applied": report["applied"],
        "foreign_ref_denied": denied,
        "legacy_scoped": legacy_scoped,
        "conflict_id": conflict_id,
        "orphan_id": orphan_id,
        "conflict_inst_ids": ids,
        "new_installation": created,
    }


def _legacy_catalog_is_scoped(fixtures):
    """Exercise the compatible warm API before migration 20 is activated."""
    a, b = fixtures["tenants"].values()
    for own, foreign in ((a, b), (b, a)):
        descriptors = json.dumps(call("GET", "/plugin-descriptors", own["admin"]))
        assert own["marker"] in descriptors and foreign["marker"] not in descriptors
        assert "MIGRATION_QUARANTINE_SENTINEL" not in descriptors
    return True


def _foreign_reference_is_denied(a, b):
    """Verify the actual database guard and absence of a forbidden side effect."""
    runtime = open_process_runtime(process="cli")
    operator = enter_system_scope("bootstrap")
    denied = False
    try:
        original = runtime.installations.get(
            a["installation"]["plugin_installation_id"]
        )
        forged = original.model_copy(
            update={
                "plugin_installation_id": "forbidden-migration-reference",
                "org_id": b["org_id"],
                "workspace_id": b["workspace_id"],
            }
        )
        try:
            runtime.installations.create(forged)
        except CheckViolation:
            denied = True
        try:
            runtime.installations.get(forged.plugin_installation_id)
        except KeyError:
            return denied
        raise AssertionError("Forbidden descriptor reference was persisted")
    finally:
        exit_system_scope(operator)
        runtime.close()


def verify():
    """After browser cleanup, quarantine must remain immutable and nonpublic."""
    require_disposable_database()
    fixture = json.loads(Path(os.environ["TENANCY_FIXTURE_FILE"]).read_text())
    migration = fixture["catalog_ownership"]
    store = ProductPostgresStore(os.environ["DATABASE_URL"], app_role="")
    try:
        with store.connection() as connection:
            reader = connection.execute(
                "SELECT reader_image FROM product_plugin_ownership_rollout "
                "WHERE version=20"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT state FROM product_plugin_ownership WHERE plugin_id IN (%s,%s)",
                (migration["conflict_id"], migration["orphan_id"]),
            ).fetchall()
            remaining = connection.execute(
                "SELECT count(*) FROM product_records "
                "WHERE collection='plugin_installations' "
                "AND payload->>'plugin_id'=%s",
                (migration["conflict_id"],),
            ).fetchone()[0]
        assert len(rows) == 2 and all(row[0] == "quarantined" for row in rows)
        assert reader == os.environ["TENANCY_CATALOG_READER_IMAGE"]
        assert remaining == 0, "Browser must remove both legacy conflicting connections"
    finally:
        store.close()
    print("Persisted catalog ownership and quarantine verified after browser cleanup.")


if __name__ == "__main__":
    verify()
