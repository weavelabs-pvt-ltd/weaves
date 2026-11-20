"""Global accounts and their memberships in tenant organizations."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from contracts.v1.base import (
    DisplayName,
    MutableOrgScopedContract,
    MutableProductContract,
    OpaqueId,
)


class AccountStatus(StrEnum):
    """Whether an account may authenticate."""

    ACTIVE = "active"
    DISABLED = "disabled"


class Account(MutableProductContract):
    """A global human identity with no implicit permission in any organization."""

    id: OpaqueId
    display_name: DisplayName
    status: AccountStatus


class AccountIdentityKind(StrEnum):
    """How an identity proves who a person is."""

    EMAIL = "email"
    OIDC = "oidc"


class AccountIdentity(MutableProductContract):
    """One login identity. Only a verified identity can authenticate an account."""

    id: OpaqueId
    account_id: OpaqueId
    kind: AccountIdentityKind
    issuer: str | None = Field(default=None, min_length=1, max_length=512)
    subject: str = Field(min_length=3, max_length=320)
    verified_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def issuer_matches_kind(self) -> AccountIdentity:
        """Keep issuer and normalized-subject rules consistent with the kind."""
        if (self.kind is AccountIdentityKind.OIDC) != (self.issuer is not None):
            raise ValueError("only OIDC identities have an issuer")
        if self.kind is AccountIdentityKind.EMAIL and self.subject != (
            self.subject.strip().casefold()
        ):
            raise ValueError("email identities are stored normalized")
        return self


class MembershipStatus(StrEnum):
    """Membership lifecycle, separate from account and user disablement."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class OrganizationMembership(MutableOrgScopedContract):
    """Links one account to one organization's existing user and principal."""

    id: OpaqueId
    account_id: OpaqueId
    user_id: OpaqueId
    principal_id: OpaqueId
    status: MembershipStatus


class AccountLoginChallenge(MutableProductContract):
    """A global, one-time email code challenge. It stores only an HMAC of the code."""

    challenge_id: OpaqueId
    email: str = Field(min_length=3, max_length=320)
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    attempts: int = Field(default=0, ge=0, le=5)
    expires_at: AwareDatetime
    # Set only for "add this email to my account" proofs; such a challenge can
    # never sign anyone in, and only that account can redeem it.
    account_id: OpaqueId | None = None

    @model_validator(mode="after")
    def email_is_normalized(self) -> AccountLoginChallenge:
        """Challenges are keyed by the normalized address, like identities."""
        if self.email != self.email.strip().casefold():
            raise ValueError("challenge emails are stored normalized")
        return self


class AccountSession(MutableProductContract):
    """An opaque account-level session stored only by its token digest."""

    id: OpaqueId
    account_id: OpaqueId
    token_digest: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    channel: Literal["bearer", "browser"] = "bearer"
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
