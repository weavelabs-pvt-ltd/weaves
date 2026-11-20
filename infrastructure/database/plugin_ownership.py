"""Explicit, bounded activation of catalog ownership after reader deployment."""

import re
from collections import Counter
from typing import Any

from infrastructure.database.plugin_ownership_sql import CLASSIFY, GUARDS, TABLES
from infrastructure.database.store import _MIGRATION_LOCK_NAME, APP_ROLE_GRANTS


def ownership_report(connection: Any) -> dict[str, Any]:
    """Report ownership metadata only; never return connector schemas or secrets."""
    applied = (
        connection.execute(
            "SELECT 1 FROM product_schema_migrations WHERE version=20"
        ).fetchone()
        is not None
    )
    rows = connection.execute(
        "SELECT plugin_id,state,org_id,reason,source_org_ids "
        "FROM product_plugin_ownership ORDER BY plugin_id"
        if applied
        else CLASSIFY
    ).fetchall()
    entries = [
        {
            "plugin_id": row[0],
            "state": row[1],
            "org_id": row[2],
            "reason": row[3],
            "source_org_ids": row[4],
        }
        for row in rows
    ]
    rollout = (
        connection.execute(
            "SELECT reader_image FROM product_plugin_ownership_rollout WHERE version=20"
        ).fetchone()
        if applied
        else None
    )
    dangling = connection.execute(
        """SELECT i.record_id,i.org_id,i.payload->>'plugin_id'
           FROM product_records i WHERE i.collection='plugin_installations'
           AND NOT EXISTS(SELECT 1 FROM product_records p
                          WHERE p.collection='plugins'
                          AND p.record_id=i.payload->>'plugin_id')
           ORDER BY i.record_id"""
    ).fetchall()
    return {
        "version": 20,
        "applied": applied,
        "reader_image": str(rollout[0]) if rollout else None,
        "counts": dict(Counter(item["state"] for item in entries)),
        "descriptors": entries,
        "unresolved_installations": [
            {
                "installation_id": row[0],
                "org_id": row[1],
                "plugin_id": row[2],
                "reason": "missing_descriptor",
            }
            for row in dangling
        ],
    }


def activate_ownership(
    connection: Any, *, reader_image: str, max_records: int = 10000
) -> dict[str, Any]:
    """Atomically backfill and activate after the compatible image is verified.

    This is deliberately separate from process startup. A failed transaction
    leaves schema 19 intact; retries cannot reassign an existing owner or clear
    quarantine. Larger catalogs require a separately rehearsed batch rollout.
    """
    if not re.fullmatch(r"(?:[^\s@]+@)?sha256:[a-f0-9]{64}", reader_image):
        raise ValueError("Record an immutable compatible reader image digest")
    if not 1 <= max_records <= 10000:
        raise ValueError("Catalog activation is bounded to at most 10000 records")
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout='10s'")
        connection.execute("SET LOCAL statement_timeout='30s'")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (_MIGRATION_LOCK_NAME,),
        )
        versions = {
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM product_schema_migrations"
            ).fetchall()
        }
        if not set(range(1, 20)) <= versions or versions - set(range(1, 22)):
            raise RuntimeError(
                "Catalog activation requires a supported schema 19 reader"
            )
        if 20 in versions:
            validate_schema(connection)
            return ownership_report(connection)
        connection.execute("LOCK TABLE product_records IN ACCESS EXCLUSIVE MODE")
        total = connection.execute(
            "SELECT count(*) FROM (SELECT 1 FROM product_records LIMIT %s) sample",
            (max_records + 1,),
        ).fetchone()[0]
        if total > max_records:
            raise RuntimeError(
                "Record store exceeds the rehearsed atomic migration limit"
            )
        report = ownership_report(connection)
        _backfill(connection, report, reader_image)
        return ownership_report(connection)


def _install_guards(connection: Any) -> None:
    """Fix guard lookup paths to the schema that owns the catalog tables."""
    from psycopg import sql

    schema = connection.execute("SELECT current_schema()").fetchone()[0]
    for statement in GUARDS:
        connection.execute(statement)
    for function in (
        "guard_plugin_record",
        "register_plugin_owner",
        "guard_plugin_ownership",
        "guard_plugin_installation_owner",
        "guard_plugin_rollout",
    ):
        connection.execute(
            sql.SQL("ALTER FUNCTION {}() SET search_path=pg_catalog,{}").format(
                sql.Identifier(schema, function), sql.Identifier(schema)
            )
        )


def validate_schema(connection: Any) -> None:
    """Refuse partially recorded or damaged ownership activation on startup."""
    tables = connection.execute(
        "SELECT to_regclass('product_plugin_ownership'), "
        "to_regclass('product_plugin_ownership_rollout')"
    ).fetchone()
    if any(value is None for value in tables):
        raise RuntimeError("Catalog migration 20 is missing its ownership tables")
    guards = connection.execute(
        """SELECT count(*) FROM pg_trigger WHERE
           ((tgrelid='product_records'::regclass AND tgname IN
             ('plugin_record_guard','plugin_record_owner','plugin_installation_owner_guard'))
            OR (tgrelid='product_plugin_ownership'::regclass
                AND tgname='plugin_ownership_guard')
            OR (tgrelid='product_plugin_ownership_rollout'::regclass
                AND tgname='plugin_rollout_guard'))
           AND tgenabled IN ('O','A')"""
    ).fetchone()[0]
    rollout = connection.execute(
        "SELECT reader_image FROM product_plugin_ownership_rollout WHERE version=20"
    ).fetchone()
    if guards != 5 or rollout is None:
        raise RuntimeError(
            "Catalog migration 20 is missing guards or its reader record"
        )


def _backfill(connection: Any, report: dict[str, Any], reader_image: str) -> None:
    """Write the reviewed plan and enable guards in one transaction."""
    for statement in TABLES:
        connection.execute(statement)
    connection.execute(APP_ROLE_GRANTS)
    for item in report["descriptors"]:
        connection.execute(
            """INSERT INTO product_plugin_ownership
               (plugin_id,state,org_id,reason,source_org_ids)
                   VALUES (%s,%s,%s,%s,%s)""",
            (
                item["plugin_id"],
                item["state"],
                item["org_id"],
                item["reason"],
                item["source_org_ids"],
            ),
        )
    connection.execute(
        """UPDATE product_records p SET org_id=o.org_id
           FROM product_plugin_ownership o WHERE p.collection='plugins'
               AND p.record_id=o.plugin_id"""
    )
    _install_guards(connection)
    from psycopg.types.json import Jsonb

    connection.execute(
        "INSERT INTO product_plugin_ownership_rollout(version,reader_image,counts) "
        "VALUES (20,%s,%s)",
        (reader_image, Jsonb(report["counts"])),
    )
    connection.execute("INSERT INTO product_schema_migrations(version) VALUES (20)")
