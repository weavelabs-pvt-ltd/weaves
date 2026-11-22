"""The waitlist is global data: ownership, docs, export and purge all agree."""

from __future__ import annotations

import json
from pathlib import Path

from infrastructure.database.row_security import (
    POLICY_FUNCTIONS,
    WAITLIST_COLLECTIONS,
)
from infrastructure.persistence.product_repositories import REPOSITORY_SPECS
from modules.auth.organization_export_rules import EXPORT_COLLECTIONS
from modules.auth.organization_retention import KEEP_COLLECTIONS, PURGE_ORDER

ROOT = Path(__file__).resolve().parents[2]


def test_waitlist_collections_are_registered_global_and_not_org_scoped():
    specs = {spec[1]: spec for spec in REPOSITORY_SPECS}
    for name in WAITLIST_COLLECTIONS:
        contract = specs[name][3]
        assert "org_id" not in contract.model_fields
        assert "workspace_id" not in contract.model_fields
        assert contract.model_config["extra"] == "forbid"


def test_tenant_export_and_purge_never_name_the_waitlist():
    assert not set(WAITLIST_COLLECTIONS) & set(EXPORT_COLLECTIONS)
    assert not set(WAITLIST_COLLECTIONS) & set(PURGE_ORDER)
    assert set(WAITLIST_COLLECTIONS) <= KEEP_COLLECTIONS


def test_only_the_waitlist_purpose_names_the_waitlist_collections():
    text = " ".join(" ".join(statement.split()) for statement in POLICY_FUNCTIONS)
    assert "WHEN 'waitlist' THEN coll = ANY (ARRAY['waitlist_entries'" in text
    for purpose in ("auth", "queue", "dispatch", "recovery", "account", "lifecycle"):
        start = text.index(f"WHEN '{purpose}' THEN")
        end = text.index("WHEN", start + 10)
        assert "waitlist" not in text[start:end], purpose


def test_ownership_inventory_declares_the_waitlist_collections_and_routes():
    registry = json.loads((ROOT / "tenancy_harness/ownership.json").read_text())
    for name in WAITLIST_COLLECTIONS:
        assert registry["collections"][name]["scope"] == "global_waitlist"
    paths = {(r["method"], r["path"]) for r in registry["routes"]}
    assert ("POST", "/api/v0/waitlist/submissions") in paths
    assert ("GET", "/api/v0/waitlist/questions") in paths
