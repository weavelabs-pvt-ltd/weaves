"""Organization-scoped OIDC connections and stable external identities."""

from pydantic import ConfigDict, Field, model_validator

from contracts.v1.base import (
    ImmutableVersionContract,
    MutableOrgScopedContract,
    OpaqueId,
    StrEnum,
)


class SsoProvider(StrEnum):
    """Supported OIDC identity providers."""

    GOOGLE = "google"
    MICROSOFT = "microsoft"
    ZOHO = "zoho"


class SsoConnectionStatus(StrEnum):
    """Administrative lifecycle for an organization connection."""

    DRAFT = "draft"
    TESTED = "tested"
    ENABLED = "enabled"
    DISABLED = "disabled"


class SsoConnection(MutableOrgScopedContract):
    """OIDC configuration; the client secret lives only in the secret store."""

    id: OpaqueId
    provider: SsoProvider
    status: SsoConnectionStatus
    issuer: str = Field(min_length=8, max_length=2048, pattern=r"^https://")
    client_id: str = Field(min_length=1, max_length=512)
    client_secret_ref: str = Field(min_length=1, max_length=256)
    tenant_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def microsoft_requires_tenant(self) -> "SsoConnection":
        """Disallow Microsoft connections without a pinned tenant."""
        if self.provider is SsoProvider.MICROSOFT and not self.tenant_id:
            raise ValueError("Microsoft SSO requires a tenant ID")
        return self


class SsoExternalIdentity(ImmutableVersionContract):
    """Immutable subject binding; email is deliberately absent from the key."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: OpaqueId
    org_id: OpaqueId
    connection_id: OpaqueId
    issuer: str = Field(min_length=8, max_length=2048, pattern=r"^https://")
    subject: str = Field(min_length=1, max_length=512)
    user_id: OpaqueId
    created_by_principal_id: OpaqueId
