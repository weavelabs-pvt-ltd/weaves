"""Organization administration metadata, separate from the stable identity payload."""

from pydantic import Field

from contracts.v1.base import MutableOrgScopedContract, OpaqueId


class OrganizationSettings(MutableOrgScopedContract):
    """One tenant's preferences and explicit human ownership."""

    id: OpaqueId
    owner_principal_id: OpaqueId
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=64)
    locale: str = Field(default="en", min_length=2, max_length=35)
    revision: int = Field(default=1, ge=1)
