"""Global accounts and their memberships in tenant organizations."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from contracts.v1.base import (
    DisplayName,
    ImmutableProductContract,
    MutableOrgScopedContract,
    MutableProductContract,
    OpaqueId,
)


class AccountStatus(StrEnum):
    """Whether an account may authenticate."""

    ACTIVE = "active"
    DISABLED = "disabled"


RoleTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]


class Account(MutableProductContract):
    """A global human identity with no implicit permission in any organization.

    ``role_title`` is the person's own free-text "role or team" label. It is
    profile text only: it grants nothing and is never copied into a tenant.
    """

    id: OpaqueId
    display_name: DisplayName
    role_title: RoleTitle | None = None
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


class AccountOAuthState(MutableProductContract):
    """A short-lived, single-use Google sign-in attempt, bound to one browser.

    Only digests of the state and of the browser-binding cookie are stored; the
    nonce and PKCE verifier stay server-side and are never sent to the browser.
    """

    id: OpaqueId
    state_digest: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    binding_digest: Annotated[
        str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    ]
    nonce: str = Field(min_length=16, max_length=128)
    code_verifier: str = Field(min_length=43, max_length=128)
    return_to: str = Field(min_length=1, max_length=512)
    expires_at: AwareDatetime


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


_Sha256 = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
]


class AccountOrganizationRequest(ImmutableProductContract):
    """Idempotency and quota record for one account-created organization.

    The id is derived from the account and key digest, so a repeated key can
    never create a second record. The raw key and request body are not stored.
    """

    id: _Sha256
    account_id: OpaqueId
    key_digest: _Sha256
    request_fingerprint: _Sha256
    organization_id: OpaqueId
