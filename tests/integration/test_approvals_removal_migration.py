"""Migration 31 deletes only what the removed approvals section owned."""

from __future__ import annotations

import json

from infrastructure.database.approvals_removal import APPROVALS_REMOVAL_MIGRATION
from infrastructure.database.store import ProductPostgresStore

_ROWS = (
    ("approval_policies", "pol-1", {"approval_policy_id": "pol-1"}),
    ("approval_requests", "req-1", {"approval_request_id": "req-1"}),
    ("tool_invocations", "inv-gated", {"approval_request_id": "req-1"}),
    ("tool_invocations", "inv-plain", {"approval_request_id": None, "run_id": "r"}),
    ("tool_invocations", "inv-clean", {"run_id": "r"}),
    ("agent_versions", "av-1", {"approval_policy_id": "pol-1", "version": 1}),
    ("roles", "role-1", {"permissions": ["approval_policies.manage", "agents.view"]}),
    ("roles", "role-2", {"permissions": ["actions.approve"]}),
    ("audit_events", "aud-1", {"action": "approval_policy.created"}),
    ("artifacts", "art-1", {"run_id": "r"}),
    ("workflow_runs", "wfr-1", {"status": "awaiting_approval"}),
)


def _payloads(connection) -> dict[str, dict]:
    rows = connection.execute("SELECT record_id, payload FROM product_records")
    return {record_id: payload for record_id, payload in rows.fetchall()}


def test_migration_31_drops_only_exclusively_owned_data(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        version, statements = APPROVALS_REMOVAL_MIGRATION
        assert version == 31
        with store.pool.connection() as connection:
            for collection, record_id, payload in _ROWS:
                connection.execute(
                    "INSERT INTO product_records (collection, record_id, payload)"
                    " VALUES (%s, %s, %s::jsonb)",
                    (collection, record_id, json.dumps(payload)),
                )
            for _ in range(2):  # idempotent: a re-run changes nothing further
                for statement in statements:
                    connection.execute(statement)
                kept = _payloads(connection)
                assert not {"pol-1", "req-1", "inv-gated"} & set(kept)
                assert kept["inv-plain"] == {"run_id": "r"}
                assert kept["inv-clean"] == {"run_id": "r"}
                assert kept["av-1"] == {"version": 1}
                assert kept["role-1"] == {"permissions": ["agents.view"]}
                assert kept["role-2"] == {"permissions": ["actions.approve"]}
                assert {"aud-1", "art-1", "wfr-1"} <= set(kept)
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'product_records'"
                ).fetchall()
            }
            assert not any("approval" in name for name in indexes)
    finally:
        store.close()
