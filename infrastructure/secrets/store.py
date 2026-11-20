"""Secret storage boundary for provider credentials.

Provider credentials are accepted only at the API edge, encrypted before durable
storage, and resolved only by a model adapter at call time.
"""

from __future__ import annotations

import hashlib
from threading import RLock
from typing import Any
from uuid import uuid4

from application.ports.secrets import SecretStore
from infrastructure.database.row_security import APP_ROLE_GRANTS
from shared.errors import SecretStoreUnavailable as SecretStoreUnavailable
from shared.request_context import effective_org_id, effective_workspace_id


def _tenant_scope() -> tuple[str, str]:
    """Return the validated organization and workspace for secret access."""
    return effective_org_id(), effective_workspace_id()


def _may_access(owner_org: str | None) -> bool:
    """Allow only the owning organization; unowned secrets stay unreadable."""
    if owner_org is None:
        return False
    return effective_org_id() == owner_org


class InMemorySecretStore:
    """Process-local store for memory-only development."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._owners: dict[str, str] = {}
        self._lock = RLock()

    def put(
        self, secret: str, *, purpose: str = "unspecified", owner_ref: str | None = None
    ) -> str:
        secret_ref = f"secret:{uuid4()}"
        with self._lock:
            self._values[secret_ref] = secret
            self._owners[secret_ref] = effective_org_id()
        return secret_ref

    def _owned(self, secret_ref: str) -> str:
        if secret_ref not in self._values or not _may_access(
            self._owners.get(secret_ref)
        ):
            raise KeyError("secret reference was not found")
        return secret_ref

    def get(self, secret_ref: str) -> str:
        with self._lock:
            return self._values[self._owned(secret_ref)]

    def replace(self, secret_ref: str, secret: str) -> None:
        with self._lock:
            self._values[self._owned(secret_ref)] = secret

    def delete(self, secret_ref: str) -> None:
        with self._lock:
            if _may_access(self._owners.get(secret_ref)):
                self._values.pop(secret_ref, None)
                self._owners.pop(secret_ref, None)


class UnavailableSecretStore:
    """Fail closed when durable mode has no encryption key configured."""

    def put(
        self, secret: str, *, purpose: str = "unspecified", owner_ref: str | None = None
    ) -> str:
        raise SecretStoreUnavailable(
            "Set WEAVES_SECRET_ENCRYPTION_KEY before adding provider credentials"
        )

    def get(self, secret_ref: str) -> str:
        raise KeyError("secret reference was not found")

    def replace(self, secret_ref: str, secret: str) -> None:
        raise SecretStoreUnavailable(
            "Set WEAVES_SECRET_ENCRYPTION_KEY before adding provider credentials"
        )

    def delete(self, secret_ref: str) -> None:
        return None


# Ownership is inferred only when every record referencing a secret belongs to
# one organization; conflicting or orphaned secrets stay unowned and unreadable.
_BACKFILL_OWNERSHIP = """
UPDATE product_secrets AS s
SET org_id = owner.org_id, workspace_id = owner.workspace_id,
    purpose = COALESCE(s.purpose, owner.purpose),
    owner_ref = COALESCE(s.owner_ref, owner.collection || ':' || owner.record_id)
FROM (
    SELECT DISTINCT ON (ref) ref, org_id, workspace_id, collection, record_id,
           CASE collection WHEN 'model_providers' THEN 'model_provider'
                           WHEN 'plugin_installations' THEN 'connector'
                           ELSE 'oauth_setup' END AS purpose
    FROM (
        SELECT payload->>'auth_ref' AS ref, org_id, workspace_id, collection,
               record_id
        FROM product_records WHERE payload->>'auth_ref' IS NOT NULL
        UNION ALL
        SELECT payload->>'secret_ref', org_id, workspace_id, collection, record_id
        FROM product_records WHERE payload->>'secret_ref' IS NOT NULL
    ) AS refs
    WHERE org_id IS NOT NULL
      AND ref IN (
        SELECT ref FROM (
            SELECT payload->>'auth_ref' AS ref, org_id FROM product_records
            UNION ALL
            SELECT payload->>'secret_ref', org_id FROM product_records
        ) AS owners
        WHERE ref IS NOT NULL AND org_id IS NOT NULL
        GROUP BY ref HAVING count(DISTINCT org_id) = 1
    )
    ORDER BY ref, record_id
) AS owner
WHERE s.secret_ref = owner.ref AND s.org_id IS NULL
"""


class EncryptedPostgresSecretStore:
    """Persist Fernet-encrypted, tenant-owned secrets using an external key."""

    def __init__(self, pool: Any, encryption_key: str) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise SecretStoreUnavailable(
                "Install backend requirements to enable encrypted PostgreSQL secrets"
            ) from exc
        try:
            self._cipher = Fernet(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SecretStoreUnavailable(
                "WEAVES_SECRET_ENCRYPTION_KEY must be a valid Fernet key"
            ) from exc
        self._key_id = hashlib.sha256(encryption_key.encode("ascii")).hexdigest()[:12]
        # A ProductPostgresStore scopes every runtime query to the caller's tenant;
        # schema setup uses its raw pool, which keeps the owning connection.
        self._pool = pool
        with getattr(pool, "pool", pool).connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS product_secrets (
                    secret_ref text PRIMARY KEY,
                    ciphertext bytea NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )"""
            )
            for column in (
                "org_id text",
                "workspace_id text",
                "purpose text",
                "owner_ref text",
                "key_id text",
            ):
                connection.execute(
                    f"ALTER TABLE product_secrets ADD COLUMN IF NOT EXISTS {column}"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS product_secrets_org_idx "
                "ON product_secrets (org_id)"
            )
            connection.execute("ALTER TABLE product_secrets ENABLE ROW LEVEL SECURITY")
            connection.execute(
                "DROP POLICY IF EXISTS tenant_isolation ON product_secrets"
            )
            connection.execute(
                """CREATE POLICY tenant_isolation ON product_secrets
                   USING (org_id = nullif(current_setting('app.org_id', true), ''))
                   WITH CHECK (
                     org_id = nullif(current_setting('app.org_id', true), ''))"""
            )
            connection.execute(APP_ROLE_GRANTS)
            connection.execute(_BACKFILL_OWNERSHIP)

    def put(
        self, secret: str, *, purpose: str = "unspecified", owner_ref: str | None = None
    ) -> str:
        secret_ref = f"secret:{uuid4()}"
        org_id, workspace_id = _tenant_scope()
        ciphertext = self._cipher.encrypt(secret.encode("utf-8"))
        with self._pool.connection() as connection:
            connection.execute(
                """INSERT INTO product_secrets
                   (secret_ref, ciphertext, org_id, workspace_id, purpose,
                    owner_ref, key_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    secret_ref,
                    ciphertext,
                    org_id,
                    workspace_id,
                    purpose,
                    owner_ref,
                    self._key_id,
                ),
            )
        return secret_ref

    def get(self, secret_ref: str) -> str:
        from cryptography.fernet import InvalidToken

        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM product_secrets "
                "WHERE secret_ref = %s AND org_id = %s",
                (secret_ref, effective_org_id()),
            ).fetchone()
        if row is None:
            raise KeyError("secret reference was not found")
        try:
            return str(self._cipher.decrypt(bytes(row[0])).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise SecretStoreUnavailable(
                "stored provider credential is unreadable"
            ) from exc

    def replace(self, secret_ref: str, secret: str) -> None:
        ciphertext = self._cipher.encrypt(secret.encode("utf-8"))
        with self._pool.connection() as connection:
            result = connection.execute(
                """UPDATE product_secrets
                   SET ciphertext = %s, key_id = %s, updated_at = now()
                   WHERE secret_ref = %s AND org_id = %s""",
                (ciphertext, self._key_id, secret_ref, effective_org_id()),
            )
            if result.rowcount == 0:
                raise KeyError("secret reference was not found")

    def delete(self, secret_ref: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM product_secrets WHERE secret_ref = %s AND org_id = %s",
                (secret_ref, effective_org_id()),
            )


class StoreSecretResolver:
    """Resolve opaque secret references through the configured store."""

    def __init__(self, store: SecretStore) -> None:
        self._store = store

    def resolve(self, auth_ref: str) -> str:
        return self._store.get(auth_ref)


class CompositeSecretResolver:
    """Support stored credentials and the legacy local environment reference."""

    def __init__(self, store: SecretStore) -> None:
        self._stored = StoreSecretResolver(store)

    def resolve(self, auth_ref: str) -> str:
        if auth_ref == "env:MODEL_API_KEY":
            import os

            return os.environ.get("MODEL_API_KEY", "")
        return self._stored.resolve(auth_ref)
