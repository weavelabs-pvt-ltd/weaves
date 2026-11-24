"""Secret store decorator that tombstones deletions and replacements first.

Connector credentials and provider keys both live in the secret store, so one
decorator covers disconnecting a connector, deleting or rotating a provider key
and OAuth setup cleanup. The tombstone is written before the change: if the log
is unwritable the change is refused (fail closed) and the operator can retry, so
a secret can never be destroyed or rotated without a durable record that a later
restore must not bring the old value back.
"""

from __future__ import annotations

from application.ports.secrets import SecretStore
from modules.recovery.tombstones import (
    SECRET_DELETED,
    SECRET_REPLACED,
    TombstoneSink,
)
from shared.request_context import effective_org_id


class TombstonedSecretStore:
    """Wrap a ``SecretStore``; reads and writes of new secrets pass through."""

    def __init__(self, inner: SecretStore, tombstones: TombstoneSink) -> None:
        """Bind the real store and the tombstone sink."""
        self._inner = inner
        self._tombstones = tombstones

    def put(
        self, secret: str, *, purpose: str = "unspecified", owner_ref: str | None = None
    ) -> str:
        """Create a secret (nothing to tombstone)."""
        return self._inner.put(secret, purpose=purpose, owner_ref=owner_ref)

    def get(self, secret_ref: str) -> str:
        """Resolve a secret."""
        return self._inner.get(secret_ref)

    def replace(self, secret_ref: str, secret: str) -> None:
        """Rotate in place: an older backup would otherwise revive the old value."""
        self._tombstones.record(
            SECRET_REPLACED,
            effective_org_id(),
            subject_kind="secret",
            subject_id=secret_ref,
        )
        self._inner.replace(secret_ref, secret)

    def delete(self, secret_ref: str) -> None:
        """Delete a secret after recording that it must stay deleted."""
        self._tombstones.record(
            SECRET_DELETED,
            effective_org_id(),
            subject_kind="secret",
            subject_id=secret_ref,
        )
        self._inner.delete(secret_ref)

    def purge_organization(self, limit: int) -> int:
        """Bulk purge is covered by the organization purge tombstone."""
        return self._inner.purge_organization(limit)
