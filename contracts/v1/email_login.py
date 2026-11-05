"""Short-lived, one-time email sign-in challenges."""

from pydantic import AwareDatetime, Field

from contracts.v1.base import MutableOrgScopedContract, OpaqueId


class EmailLoginChallenge(MutableOrgScopedContract):
    """Persist only an HMAC of a short email verification code."""

    challenge_id: OpaqueId
    user_id: OpaqueId
    principal_id: OpaqueId
    email: str = Field(min_length=3, max_length=320)
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    attempts: int = Field(default=0, ge=0, le=5)
    expires_at: AwareDatetime
