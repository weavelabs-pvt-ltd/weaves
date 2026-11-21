"""Secret storage contract consumed by model and integration use cases."""

from typing import Protocol


class SecretStore(Protocol):
    """Store opaque references while keeping secret values inside adapters."""

    def put(
        self, secret: str, *, purpose: str = "unspecified", owner_ref: str | None = None
    ) -> str:
        """Persist a secret for the current tenant and return its opaque reference.

        ``purpose`` and ``owner_ref`` record what the secret is for and which
        resource owns it, so it can be audited, rotated and cleaned up.
        """
        ...

    def replace(self, secret_ref: str, secret: str) -> None:
        """Replace the secret stored at an existing reference."""
        ...

    def get(self, secret_ref: str) -> str:
        """Resolve an opaque reference to a secret value."""
        ...

    def delete(self, secret_ref: str) -> None:
        """Delete the secret stored at an opaque reference."""
        ...

    def purge_organization(self, limit: int) -> int:
        """Delete up to ``limit`` secrets owned by the current organization."""
        ...
