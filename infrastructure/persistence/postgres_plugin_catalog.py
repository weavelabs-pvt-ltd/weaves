"""SQL-scoped plugin catalog over unchanged descriptor JSON records."""

from typing import Any

from contracts.v1 import PluginDescriptor
from contracts.v1.plugin_visibility import BUILTIN_PLUGIN_TYPES
from infrastructure.persistence.postgres_repository import PostgresRepository

_BUILTIN = """((p.record_id='github' AND p.payload->>'plugin_type'='github')
 OR (p.record_id='slack' AND p.payload->>'plugin_type'='slack')
 OR (p.record_id='jira' AND p.payload->>'plugin_type'='jira_cloud'))"""


class PostgresPluginCatalog(PostgresRepository[PluginDescriptor]):
    """Use persisted scope after activation, with safe legacy reads beforehand."""

    def __init__(self, store: Any) -> None:
        """Bind catalog operations to the process store and its unit of work."""
        super().__init__(store, "plugins", "plugin_id", PluginDescriptor)

    @staticmethod
    def _migrated(connection: Any) -> bool:
        """Observe operator activation without requiring every process to restart."""
        # Pin the catalog's schema epoch until this transaction finishes. The
        # operator takes ACCESS EXCLUSIVE before changing ownership or the marker.
        connection.execute("LOCK TABLE product_records IN ACCESS SHARE MODE")
        return (
            connection.execute(
                "SELECT 1 FROM product_schema_migrations WHERE version=20"
            ).fetchone()
            is not None
        )

    def create_owned_descriptor(
        self, descriptor: PluginDescriptor, org_id: str
    ) -> PluginDescriptor:
        """Persist owner columns while keeping the legacy wire payload identical."""
        from psycopg.errors import UniqueViolation
        from psycopg.types.json import Jsonb

        if not org_id or descriptor.plugin_id in BUILTIN_PLUGIN_TYPES:
            raise ValueError("Custom descriptor requires an organization and custom ID")
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """INSERT INTO product_records
                       (collection,record_id,org_id,payload)
                       VALUES ('plugins',%s,%s,%s)""",
                    (
                        descriptor.plugin_id,
                        org_id,
                        Jsonb(descriptor.model_dump(mode="json")),
                    ),
                )
        except UniqueViolation as exc:
            raise ValueError("plugin_id already exists") from exc
        return self._clone(descriptor)

    def put(self, record: PluginDescriptor) -> PluginDescriptor:
        """Update descriptor content without erasing or reassigning its owner."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as connection:
            result = connection.execute(
                """UPDATE product_records SET payload=%s,updated_at=now()
                   WHERE collection='plugins' AND record_id=%s""",
                (Jsonb(record.model_dump(mode="json")), record.plugin_id),
            )
        if result.rowcount == 0:
            return super().create(record)
        return self._clone(record)

    def _visible_query(
        self, connection: Any, org_id: str
    ) -> tuple[str, tuple[str, ...]]:
        """Scope in SQL rather than loading other organizations' schemas."""
        if self._migrated(connection):
            return (
                """SELECT p.payload FROM product_records p
                   JOIN product_plugin_ownership o ON o.plugin_id=p.record_id
                   WHERE p.collection='plugins' AND
                   ((p.org_id=%s AND o.state='owned' AND o.org_id=p.org_id)
                    OR (p.org_id IS NULL AND o.state='builtin'))""",
                (org_id,),
            )
        return (
            "SELECT p.payload FROM product_records p WHERE p.collection='plugins' "
            "AND p.payload->>'plugin_id'=p.record_id "
            "AND (p.record_id NOT IN ('github','slack','jira') OR p.org_id IS NULL) "
            "AND ("
            + _BUILTIN
            + """ OR (
              (p.org_id=%s OR (p.org_id IS NULL AND EXISTS (
                SELECT 1 FROM product_records i
                WHERE i.collection='plugin_installations'
                AND i.payload->>'plugin_id'=p.record_id AND i.org_id=%s
              )))
              AND NOT EXISTS (
                SELECT 1 FROM product_records i
                WHERE i.collection='plugin_installations'
                AND i.payload->>'plugin_id'=p.record_id AND i.org_id IS DISTINCT FROM %s
              )
            ))""",
            (org_id, org_id, org_id),
        )

    def visible_descriptors(self, org_id: str) -> tuple[PluginDescriptor, ...]:
        """Read only public or owned rows, excluding persisted quarantine."""
        with self._pool.connection() as connection:
            query, parameters = self._visible_query(connection, org_id)
            rows = connection.execute(
                query + " ORDER BY p.record_id", parameters
            ).fetchall()
        return tuple(self._deserialize(row[0]) for row in rows)

    def visible_descriptor(self, plugin_id: str, org_id: str) -> PluginDescriptor:
        """Load one visible definition at the database boundary."""
        with self._pool.connection() as connection:
            query, parameters = self._visible_query(connection, org_id)
            row = connection.execute(
                query + " AND p.record_id=%s", (*parameters, plugin_id)
            ).fetchone()
        if row is None:
            raise KeyError("Plugin descriptor not found in organization scope")
        return self._deserialize(row[0])

    def ownership_state(self, plugin_id: str) -> str | None:
        """Return persisted classification without exposing another tenant's schema."""
        with self._pool.connection() as connection:
            if not self._migrated(connection):
                return None
            row = connection.execute(
                "SELECT state FROM product_plugin_ownership WHERE plugin_id=%s",
                (plugin_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def has_installations(self, plugin_id: str) -> bool:
        """Use the reference index instead of materializing connection payloads."""
        with self._pool.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM product_records "
                    "WHERE collection='plugin_installations' "
                    "AND payload->>'plugin_id'=%s LIMIT 1",
                    (plugin_id,),
                ).fetchone()
                is not None
            )

    def delete_unreferenced_descriptor(self, plugin_id: str) -> bool:
        """Make cleanup conditional and tolerate a concurrent new reference."""
        from psycopg.errors import CheckViolation

        with self._pool.connection() as connection:
            try:
                with connection.transaction():
                    ownership = (
                        "AND EXISTS(SELECT 1 FROM product_plugin_ownership o "
                        "WHERE o.plugin_id=p.record_id AND o.state='owned') "
                        if self._migrated(connection)
                        else ""
                    )
                    row = connection.execute(
                        "DELETE FROM product_records p WHERE p.collection='plugins' "
                        "AND p.record_id=%s "
                        "AND p.record_id NOT IN ('github','slack','jira') "
                        + ownership
                        + "AND NOT EXISTS(SELECT 1 FROM product_records i "
                        "WHERE i.collection='plugin_installations' "
                        "AND i.payload->>'plugin_id'=p.record_id) "
                        "RETURNING p.record_id",
                        (plugin_id,),
                    ).fetchone()
                    return row is not None
            except CheckViolation:
                return False
