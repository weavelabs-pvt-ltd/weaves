"""Discover provider catalogs and reconcile model profiles."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol, cast

from application.ports.repository import Repository
from clients.model_providers.gateway import ModelAdapter, ModelGatewayError
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    ModelProfile,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
    Permission,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import effective_org_id, effective_principal_id


class ModelCatalogAdapter(ModelAdapter, Protocol):
    """Adapter capability required for discovering provider models."""

    def list_models(
        self, provider: ModelProviderContract
    ) -> tuple[dict[str, str], ...]:
        """List selectable model identifiers offered by the provider."""
        ...


class ModelCatalogService:
    """Discover provider models and persist matching selectable profiles."""

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProviderContract],
        model_adapters: Mapping[ModelProviderType, ModelAdapter],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind catalog persistence, provider adapters, and auth operations."""
        self.audit_events = audit_events
        self.model_profiles = model_profiles
        self.providers = providers
        self.model_adapters = model_adapters
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize_organization = authorize_organization
        self._open_unit_of_work = unit_of_work

    def refresh_provider_model_catalog(
        self, provider_id: str
    ) -> tuple[ModelProfile, ...]:
        """Validate provider credentials and persist selectable model profiles."""
        provider = self._active_provider_for_catalog(provider_id)
        adapter = self._catalog_adapter(provider)
        model_ids = self._available_model_ids(adapter.list_models(provider))
        existing_profiles = self._existing_provider_profiles(provider.id)
        timestamp = _now()
        updated_provider, profiles, new_profiles, updated_profiles = (
            self._reconcile_provider_profiles(
                provider, model_ids, existing_profiles, timestamp
            )
        )
        actor = effective_principal_id("local-developer")
        with self._open_unit_of_work():
            self.providers.put(updated_provider)
            for profile in updated_profiles:
                self.model_profiles.put(profile)
            for profile in new_profiles:
                self.model_profiles.create(profile)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=provider.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=actor,
                    action="model_provider.catalog_refreshed",
                    target_type="model_provider",
                    target_id=provider.id,
                    summary="Model provider catalog refreshed",
                    request_id=provider.id,
                    metadata={"model_count": len(model_ids)},
                    created_at=timestamp,
                )
            )
        return tuple(profiles)

    def _active_provider_for_catalog(self, provider_id: str) -> ModelProviderContract:
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize_organization(
            effective_principal_id("local-developer"), Permission.MODELS_MANAGE
        )
        provider = self.providers.get_scoped(provider_id, effective_org_id())
        if provider.status is not ModelProviderStatus.ACTIVE:
            raise ModelGatewayError(
                "model.provider_disabled", "Model provider is disabled"
            )
        return provider

    def _catalog_adapter(self, provider: ModelProviderContract) -> ModelCatalogAdapter:
        adapter = self.model_adapters.get(provider.provider_type)
        if adapter is None or not hasattr(adapter, "list_models"):
            raise ModelGatewayError(
                "model.catalog_unsupported",
                "Selected provider does not support model discovery",
            )
        return cast(ModelCatalogAdapter, adapter)

    def _existing_provider_profiles(self, provider_id: str) -> dict[str, ModelProfile]:
        return {
            profile.model: profile
            for profile in self.model_profiles.list_scoped(effective_org_id())
            if profile.provider_id == provider_id
        }

    def _reconcile_provider_profiles(
        self,
        provider: ModelProviderContract,
        model_ids: tuple[str, ...],
        existing_profiles: dict[str, ModelProfile],
        timestamp: datetime,
    ) -> tuple[
        ModelProviderContract,
        list[ModelProfile],
        list[ModelProfile],
        list[ModelProfile],
    ]:
        profile_models = set(existing_profiles)
        retained_models = [
            model for model in provider.allowed_models if model in profile_models
        ]
        allowed_models = list(
            dict.fromkeys([*retained_models, *sorted(profile_models), *model_ids])
        )
        updated_provider = ModelProviderContract.model_validate(
            provider.model_copy(update={"allowed_models": allowed_models}).model_dump()
        )
        profiles: list[ModelProfile] = []
        new_profiles: list[ModelProfile] = []
        updated_profiles = self._disabled_profiles(
            existing_profiles, model_ids, timestamp
        )
        for model_id in model_ids:
            profile = existing_profiles.get(model_id)
            if profile is None:
                profile = self._new_model_profile(provider, model_id, timestamp)
                new_profiles.append(profile)
            elif profile.status is ModelProfileStatus.DISABLED:
                profile = profile.model_copy(
                    update={
                        "status": ModelProfileStatus.ACTIVE,
                        "updated_at": timestamp,
                    }
                )
                updated_profiles.append(profile)
            profiles.append(profile)
        return updated_provider, profiles, new_profiles, updated_profiles

    @staticmethod
    def _disabled_profiles(
        existing_profiles: dict[str, ModelProfile],
        model_ids: tuple[str, ...],
        timestamp: datetime,
    ) -> list[ModelProfile]:
        return [
            profile.model_copy(
                update={
                    "status": ModelProfileStatus.DISABLED,
                    "updated_at": timestamp,
                }
            )
            for profile in existing_profiles.values()
            if profile.model not in model_ids
            and profile.status is ModelProfileStatus.ACTIVE
        ]

    @staticmethod
    def _new_model_profile(
        provider: ModelProviderContract, model_id: str, timestamp: datetime
    ) -> ModelProfile:
        profile_id = (
            "model-"
            + hashlib.sha256((provider.id + model_id).encode("utf-8")).hexdigest()[:24]
        )
        return ModelProfile(
            id=profile_id,
            org_id=effective_org_id(),
            provider_id=provider.id,
            model=model_id,
            display_name=f"{provider.display_name} · {model_id}"[:160],
            status=ModelProfileStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def _available_model_ids(models: tuple[dict[str, str], ...]) -> tuple[str, ...]:
        model_ids = tuple(
            dict.fromkeys(model["id"] for model in models if len(model["id"]) <= 160)
        )[:500]
        if not model_ids:
            raise ModelGatewayError(
                "model.catalog_empty", "Provider returned no selectable models"
            )
        return model_ids
