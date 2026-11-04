"""Default model profile selection for model-dependent capabilities."""

from application.ports.repository import Repository
from contracts.v1 import (
    ModelAccessMode,
    ModelProfile,
    ModelProfileStatus,
    OrganizationModelConfiguration,
)
from shared.request_context import effective_org_id


def default_active_model_profile(
    profiles: Repository[ModelProfile],
    configurations: Repository[OrganizationModelConfiguration] | None = None,
    org_id: str | None = None,
) -> ModelProfile:
    """Return the first active model profile available in the organization."""
    selected_org = org_id or effective_org_id()
    if configurations is not None:
        try:
            configuration = configurations.get_scoped(selected_org, selected_org)
        except KeyError:
            configuration = None
        if configuration and configuration.default_model_profile_id:
            profile = profiles.get_scoped(
                configuration.default_model_profile_id, selected_org
            )
            if profile.status is ModelProfileStatus.ACTIVE:
                return profile
            raise ValueError("configured default model profile is disabled")
        if configuration and configuration.mode is not None:
            raise ValueError("choose an active default model profile")
    ordered_profiles = sorted(
        profiles.list_scoped(selected_org), key=lambda profile: profile.id
    )
    selected = next(
        (
            profile
            for profile in ordered_profiles
            if profile.status is ModelProfileStatus.ACTIVE
        ),
        None,
    )
    if selected is None:
        raise ValueError("configure an active model provider before creating agents")
    return selected


def configured_model_access_mode(
    configurations: Repository[OrganizationModelConfiguration],
    org_id: str | None = None,
) -> ModelAccessMode | None:
    """Return the access mode selected by an organization, if any."""
    selected_org = org_id or effective_org_id()
    try:
        return configurations.get_scoped(selected_org, selected_org).mode
    except KeyError:
        return None
