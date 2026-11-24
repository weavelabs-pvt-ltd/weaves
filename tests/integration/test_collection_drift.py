"""Every stored collection is a registered one; removed ones are listed to purge."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import psycopg
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.approvals_removal import REMOVED_COLLECTIONS
from infrastructure.database.legacy_purge import PURGED_COLLECTIONS
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import REPOSITORY_SPECS
from modules.auth.organization_retention import KEEP_COLLECTIONS, PURGE_ORDER
from tests.support.lifecycle_http import populated_world

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = {spec[1] for spec in REPOSITORY_SPECS}


def test_populated_database_holds_only_registered_collections(
    isolated_postgres_url, monkeypatch
):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    store = ProductPostgresStore(pool=open_postgres_pool(isolated_postgres_url))
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    populated_world(runtime)
    with psycopg.connect(isolated_postgres_url) as connection:
        stored = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT collection FROM product_records"
            )
        }
    store.close()
    assert len(stored) > 20
    assert stored <= REGISTRY, f"unregistered collections: {stored - REGISTRY}"
    assert not stored & (set(PURGED_COLLECTIONS) | set(REMOVED_COLLECTIONS))


def test_sql_and_retention_lists_name_only_registered_collections():
    """Source that names a collection must name a live one (or a purged one)."""
    assert set(PURGE_ORDER) | KEEP_COLLECTIONS <= REGISTRY
    named: set[str] = set()
    for folder in ("infrastructure", "modules", "application", "apps"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text()
            named |= set(re.findall(r"collection\s*=\s*'([a-z_]+)'", text))
    # Migrations 6, 15, 19 and 22 name purged collections as history; the rest
    # of the code must not.
    unknown = named - REGISTRY - set(PURGED_COLLECTIONS) - set(REMOVED_COLLECTIONS)
    assert not unknown, f"SQL names unknown collections: {unknown}"
