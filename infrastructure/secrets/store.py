"""Secret storage boundary for provider credentials.

Provider credentials are accepted only at the API edge, encrypted before durable
storage, and resolved only by a model adapter at call time.
"""

from __future__ import annotations

from threading import RLock
from typing import Any
from uuid import uuid4

from application.ports.secrets import SecretStore
from shared.errors import SecretStoreUnavailable as SecretStoreUnavailable


class InMemorySecretStore:
    """Process-local store for memory-only development."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = RLock()

    def put(self, secret: str) -> str:
        secret_ref = f"secret:{uuid4()}"
        with self._lock:
            self._values[secret_ref] = secret
        return secret_ref

    def get(self, secret_ref: str) -> str:
        with self._lock:
            try:
                return self._values[secret_ref]
            except KeyError as exc:
                raise KeyError("secret reference was not found") from exc

    def replace(self, secret_ref: str, secret: str) -> None:
        with self._lock:
            if secret_ref not in self._values:
                raise KeyError("secret reference was not found")
            self._values[secret_ref] = secret

    def delete(self, secret_ref: str) -> None:
        with self._lock:
            self._values.pop(secret_ref, None)


class UnavailableSecretStore:
    """Fail closed when durable mode has no encryption key configured."""

    def put(self, secret: str) -> str:
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


class EncryptedPostgresSecretStore:
    """Persist Fernet-encrypted secrets in PostgreSQL using an external key."""

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
        self._pool = pool
        with self._pool.connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS product_secrets (
                    secret_ref text PRIMARY KEY,
                    ciphertext bytea NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )"""
            )

    def put(self, secret: str) -> str:
        secret_ref = f"secret:{uuid4()}"
        ciphertext = self._cipher.encrypt(secret.encode("utf-8"))
        with self._pool.connection() as connection:
            connection.execute(
                "INSERT INTO product_secrets (secret_ref, ciphertext) VALUES (%s, %s)",
                (secret_ref, ciphertext),
            )
        return secret_ref

    def get(self, secret_ref: str) -> str:
        from cryptography.fernet import InvalidToken

        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM product_secrets WHERE secret_ref = %s",
                (secret_ref,),
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
                   SET ciphertext = %s, updated_at = now()
                   WHERE secret_ref = %s""",
                (ciphertext, secret_ref),
            )
            if result.rowcount == 0:
                raise KeyError("secret reference was not found")

    def delete(self, secret_ref: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM product_secrets WHERE secret_ref = %s", (secret_ref,)
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
