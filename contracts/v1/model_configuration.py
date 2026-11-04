"""Organization-level ownership and default selection for model access."""

from contracts.v1.base import MutableOrgScopedContract, OpaqueId, StrEnum


class ModelAccessMode(StrEnum):
    """Select who owns the provider credentials and model bill."""

    CUSTOMER_MANAGED = "customer_managed"
    WEAVES_MANAGED = "weaves_managed"


class OrganizationModelConfiguration(MutableOrgScopedContract):
    """Persist one organization's exclusive model access mode and default."""

    org_id: OpaqueId
    mode: ModelAccessMode | None = None
    default_provider_id: OpaqueId | None = None
    default_model_profile_id: OpaqueId | None = None
