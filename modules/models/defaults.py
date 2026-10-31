"""Default model profile selection for model-dependent capabilities."""

from application.ports.repository import Repository
from contracts.v1 import ModelProfile, ModelProfileStatus
from shared.request_context import effective_org_id


def default_active_model_profile(
    profiles: Repository[ModelProfile], org_id: str | None = None
) -> ModelProfile:
    """Return the first active model profile available in the organization."""
    selected_org = org_id or effective_org_id()
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
