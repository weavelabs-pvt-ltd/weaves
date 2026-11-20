"""Prove legacy descriptor ownership survives PostgreSQL persistence."""

from application.descriptor_catalog import scoped_descriptor, visible_descriptors
from infrastructure.database.store import ProductPostgresStore
from tests.contracts.test_plugin_workflow_contracts import descriptor, installation
from tests.support.runtime import LocalPlatformRuntime


def test_legacy_descriptor_references_remain_scoped_after_restart(
    isolated_postgres_url,
):
    store = ProductPostgresStore(isolated_postgres_url)
    runtime = LocalPlatformRuntime(postgres_store=store)
    try:
        runtime.bootstrap()
        runtime.plugins.create(descriptor())
        runtime.installations.create(installation())
        with store.connection() as connection:
            row = connection.execute(
                "SELECT org_id, payload FROM product_records "
                "WHERE collection='plugins' AND record_id='custom-mcp'"
            ).fetchone()
        assert row[0] is None
        assert "org_id" not in row[1]
    finally:
        runtime.close()
    reopened = LocalPlatformRuntime(
        postgres_store=ProductPostgresStore(isolated_postgres_url)
    )
    try:
        reopened.bootstrap()
        assert scoped_descriptor(
            reopened.plugins, reopened.installations, "custom-mcp", "org-a"
        )
        assert "custom-mcp" not in {
            item.plugin_id
            for item in visible_descriptors(
                reopened.plugins, reopened.installations, "org-b"
            )
        }
        reopened.installations.create(
            installation(
                plugin_installation_id="ambiguous-owner",
                org_id="org-b",
                workspace_id="workspace-b",
            )
        )
        for org_id in ("org-a", "org-b"):
            assert "custom-mcp" not in {
                item.plugin_id
                for item in visible_descriptors(
                    reopened.plugins, reopened.installations, org_id
                )
            }
        assert reopened.plugins.get("custom-mcp").display_name == "Custom MCP"
    finally:
        reopened.close()
