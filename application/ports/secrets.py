"""Secret storage contract consumed by model and integration use cases."""

from typing import Protocol


class SecretStore(Protocol):
    """Store opaque references while keeping secret values inside adapters."""

    def put(self, secret: str) -> str:
        """Persist a secret and return its opaque reference."""
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
