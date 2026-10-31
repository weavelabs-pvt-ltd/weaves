"""Human authentication credentials and revocable bearer sessions."""

from typing import Annotated

from pydantic import AwareDatetime, StringConstraints, model_validator

from contracts.v1.base import MutableOrgScopedContract, OpaqueId, StrEnum


class UserPasswordCredential(MutableOrgScopedContract):
    """A per-user password verifier; plaintext passwords are never persisted."""

    id: OpaqueId
    password_hash: Annotated[str, StringConstraints(min_length=32, max_length=256)]
    changed_by_principal_id: OpaqueId


class UserSessionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class UserSession(MutableOrgScopedContract):
    """A high-entropy, expiring user session stored by token digest."""

    id: OpaqueId
    principal_id: OpaqueId
    token_digest: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    status: UserSessionStatus
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def revocation_matches_status(self) -> "UserSession":
        if (self.status is UserSessionStatus.REVOKED) != (self.revoked_at is not None):
            raise ValueError("revoked sessions require revoked_at")
        return self


__all__ = ["UserPasswordCredential", "UserSession", "UserSessionStatus"]
