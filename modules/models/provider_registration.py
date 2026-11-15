"""Persist provider credentials, initial profiles and defaults atomically."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    ModelAccessMode,
    ModelProfile,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
    OrganizationModelConfiguration,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import effective_org_id

if TYPE_CHECKING:
    from modules.models.runtime import ModelService


def store_model_provider(
    service: ModelService,
    defaults: Mapping[ModelProviderType, str],
    provider_id: str,
    requested_by_principal_id: str,
    secret_ref: str,
    selected_model: str,
    base_url: str | None,
    display_name: str,
    initial_model: str | None,
    provider_type: ModelProviderType,
    credential_last4: str | None = None,
) -> ModelProviderContract:
    """Persist a model provider and its initial profile."""
    try:
        provider = ModelProviderContract(
            id=provider_id,
            org_id=effective_org_id(),
            provider_type=provider_type,
            display_name=display_name,
            allowed_models=[selected_model],
            auth_ref=secret_ref,
            credential_last4=credential_last4,
            base_url=(base_url.strip() if base_url else defaults[provider_type]),
            status=ModelProviderStatus.ACTIVE,
        )
        timestamp = _now()
        profile = _build_initial_profile(
            provider, initial_model, selected_model, timestamp
        )
        with service._open_unit_of_work():
            service.providers.create(provider)
            if profile is not None:
                service.model_profiles.create(profile)
            config = _provider_configuration(service, provider, profile, timestamp)
            service.model_configurations.put(config)
            service.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=requested_by_principal_id,
                    action="model_provider.created",
                    target_type="model_provider",
                    target_id=provider_id,
                    summary="Model provider configured",
                    request_id=provider_id,
                    metadata={"provider_type": provider_type.value},
                    created_at=timestamp,
                )
            )
    except Exception:
        service.secret_store.delete(secret_ref)
        raise
    return provider


def _provider_configuration(
    service: ModelService,
    provider: ModelProviderContract,
    profile: ModelProfile | None,
    timestamp: datetime,
) -> OrganizationModelConfiguration:
    """Preserve the existing default unless an initial profile was selected."""
    existing_config = service._configuration_or_none()
    return OrganizationModelConfiguration(
        org_id=effective_org_id(),
        mode=ModelAccessMode.CUSTOMER_MANAGED,
        default_provider_id=(
            provider.id
            if profile is not None
            else (
                existing_config.default_provider_id
                if existing_config
                and existing_config.mode is ModelAccessMode.CUSTOMER_MANAGED
                else provider.id
            )
        ),
        default_model_profile_id=(
            profile.id
            if profile is not None
            else (
                existing_config.default_model_profile_id
                if existing_config
                and existing_config.mode is ModelAccessMode.CUSTOMER_MANAGED
                else None
            )
        ),
        created_at=(existing_config.created_at if existing_config else timestamp),
        updated_at=timestamp,
    )


def _build_initial_profile(
    provider: ModelProviderContract,
    initial_model: str | None,
    selected_model: str,
    timestamp: datetime,
) -> ModelProfile | None:
    """Create a profile for an explicitly selected model, if supplied."""
    if not initial_model:
        return None
    model_hash = hashlib.sha256((provider.id + selected_model).encode("utf-8"))
    return ModelProfile(
        id="model-" + model_hash.hexdigest()[:24],
        org_id=effective_org_id(),
        provider_id=provider.id,
        model=selected_model,
        display_name=f"{provider.display_name} · {selected_model}"[:160],
        status=ModelProfileStatus.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )
