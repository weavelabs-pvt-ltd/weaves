"""Human authentication credentials and revocable bearer sessions."""

from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)

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
    channel: Literal["bearer", "browser"] = "bearer"
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_serializer(mode="wrap")
    def serialize_session(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Keep bearer writes readable by the pre-browser release.

        Browser sessions retain their explicit channel across repository cloning;
        omitting that value would incorrectly downgrade them into bearer tokens.
        """
        payload: dict[str, Any] = dict(handler(self))
        if self.channel == "bearer":
            payload.pop("channel", None)
        return payload

    @model_validator(mode="after")
    def revocation_matches_status(self) -> "UserSession":
        if (self.status is UserSessionStatus.REVOKED) != (self.revoked_at is not None):
            raise ValueError("revoked sessions require revoked_at")
        return self


__all__ = ["UserPasswordCredential", "UserSession", "UserSessionStatus"]
