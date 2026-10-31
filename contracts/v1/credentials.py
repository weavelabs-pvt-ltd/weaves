"""Authentication credential contracts for local service identities."""

from typing import Annotated

from pydantic import AwareDatetime, StringConstraints, model_validator

from contracts.v1.base import (
    MutableOrgScopedContract,
    OpaqueId,
    StrEnum,
)


class ApiCredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ApiCredential(MutableOrgScopedContract):
    """Persist only a one-way digest for a high-entropy bearer credential."""

    id: OpaqueId
    token_digest: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    principal_id: OpaqueId
    status: ApiCredentialStatus
    expires_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def revocation_matches_status(self) -> "ApiCredential":
        if (self.status is ApiCredentialStatus.REVOKED) != (
            self.revoked_at is not None
        ):
            raise ValueError("revoked credentials require revoked_at")
        return self


__all__ = ["ApiCredential", "ApiCredentialStatus"]
