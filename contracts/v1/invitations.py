"""One-time workspace user invitation contracts."""

from typing import Annotated

from pydantic import AwareDatetime, StringConstraints, model_validator

from contracts.v1.base import (
    MutableWorkspaceScopedContract,
    OpaqueId,
    StrEnum,
)


class UserInvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"


class UserInvitation(MutableWorkspaceScopedContract):
    """An expiring invite whose bearer token is stored only as a digest."""

    invitation_id: OpaqueId
    user_id: OpaqueId
    principal_id: OpaqueId
    email: Annotated[str, StringConstraints(min_length=3, max_length=320)]
    token_digest: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    status: UserInvitationStatus
    expires_at: AwareDatetime
    accepted_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def timestamps_match_status(self) -> "UserInvitation":
        if self.status is UserInvitationStatus.PENDING and (
            self.accepted_at is not None or self.revoked_at is not None
        ):
            raise ValueError("pending invitations cannot have terminal timestamps")
        if self.status is UserInvitationStatus.ACCEPTED and (
            self.accepted_at is None or self.revoked_at is not None
        ):
            raise ValueError("accepted invitations require only accepted_at")
        if self.status is UserInvitationStatus.REVOKED and (
            self.revoked_at is None or self.accepted_at is not None
        ):
            raise ValueError("revoked invitations require only revoked_at")
        return self


__all__ = ["UserInvitation", "UserInvitationStatus"]
