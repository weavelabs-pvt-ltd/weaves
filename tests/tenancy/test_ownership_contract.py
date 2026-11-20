"""Require a reviewed ownership rule for each persisted collection and API."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.app import create_app
from infrastructure.persistence.product_repositories import REPOSITORY_SPECS
from tests.support.runtime import LocalPlatformRuntime

REGISTRY = Path(__file__).resolve().parents[2] / "tenancy_harness/ownership.json"


def test_all_persisted_collections_have_explicit_ownership_rules():
    registry = json.loads(REGISTRY.read_text())
    assert set(registry["collections"]) == {spec[1] for spec in REPOSITORY_SPECS}
    for _, collection, _, contract in REPOSITORY_SPECS:
        rule = registry["collections"][collection]
        assert rule["contract"] == contract.__name__
        assert rule["scope"] in {
            "organization",
            "workspace",
            "mixed_catalog",
            "global_account",
        }
        assert rule["privacy"] and rule["system_access"] and rule["lifecycle"]
        assert set(rule["scope_keys"]) <= set(contract.model_fields)
    assert registry["collections"]["plugins"]["current_gap"]


def test_all_http_operations_have_explicit_scope_and_authentication_rules():
    registry = json.loads(REGISTRY.read_text())
    runtime = LocalPlatformRuntime(postgres_store=None)
    with TestClient(create_app(runtime)) as client:
        actual = {
            (method, route.path)
            for route in client.app.routes
            if route.path.startswith("/api/v0/")
            for method in route.methods
        }
    entries = registry["routes"]
    declared = {(rule["method"], rule["path"]) for rule in entries}
    assert len(entries) == len(declared), "Duplicate API ownership entry"
    assert actual == declared, "New/removed route requires an ownership review"
    for rule in entries:
        assert rule["scope"] and rule["rule"] and rule["lifecycle"]
