"""Model-provider and model-profile lifecycle use cases."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from decimal import Decimal

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
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
from modules.models.catalog import ModelCatalogService
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import effective_org_id, effective_principal_id


class ModelService:
    """Manage model providers and profiles through explicit capability ports."""

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProviderContract],
        secret_store: SecretStore,
        catalog_service: ModelCatalogService,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        default_model_profile_id: str,
    ) -> None:
        """Bind repositories, security operations, and provider adapters."""
        self.audit_events = audit_events
        self.model_profiles = model_profiles
        self.providers = providers
        self.secret_store = secret_store
        self.catalog_service = catalog_service
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._authorize_organization = authorize_organization
        self._open_unit_of_work = unit_of_work
        self.default_model_profile_id = default_model_profile_id

    def list_model_profiles(self) -> tuple[ModelProfile, ...]:
        """List organization profiles with the configured default first."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize(
            effective_principal_id("local-developer"), Permission.MODELS_VIEW
        )
        profiles = self.model_profiles.list_scoped(effective_org_id())
        return tuple(
            sorted(profiles, key=lambda item: item.id != self.default_model_profile_id)
        )

    def list_model_providers(self) -> tuple[ModelProviderContract, ...]:
        """List organization providers without exposing secret references."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize(
            effective_principal_id("local-developer"), Permission.MODELS_VIEW
        )
        return self.providers.list_scoped(effective_org_id())

    def create_model_provider(
        self,
        *,
        provider_type: ModelProviderType,
        display_name: str,
        api_key: str,
        base_url: str | None = None,
        initial_model: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> ModelProviderContract:
        """Register a provider and store its key behind an opaque secret ref."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize_organization(
            requested_by_principal_id, Permission.MODELS_MANAGE
        )
        if provider_type not in {
            ModelProviderType.OPENAI,
            ModelProviderType.OPENAI_COMPATIBLE,
            ModelProviderType.ANTHROPIC,
            ModelProviderType.GEMINI,
        }:
            raise ValueError("selected provider type is not available in v0")
        if not api_key.strip():
            raise ValueError("provider API key is required")
        defaults = {
            ModelProviderType.OPENAI: "https://api.openai.com/v1",
            ModelProviderType.ANTHROPIC: "https://api.anthropic.com",
            ModelProviderType.GEMINI: "https://generativelanguage.googleapis.com/v1beta",
        }
        if provider_type is ModelProviderType.OPENAI_COMPATIBLE and not base_url:
            raise ValueError("base_url is required for OpenAI-compatible providers")
        selected_model = initial_model.strip() if initial_model else "catalog-pending"
        secret_ref = self.secret_store.put(api_key.strip())
        provider_id = _id()
        provider = self._store_model_provider(
            defaults,
            provider_id,
            requested_by_principal_id,
            secret_ref,
            selected_model,
            base_url,
            display_name,
            initial_model,
            provider_type,
        )
        return provider

    def _store_model_provider(
        self,
        defaults: Mapping[ModelProviderType, str],
        provider_id: str,
        requested_by_principal_id: str,
        secret_ref: str,
        selected_model: str,
        base_url: str | None,
        display_name: str,
        initial_model: str | None,
        provider_type: ModelProviderType,
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
                base_url=(base_url.strip() if base_url else defaults[provider_type]),
                status=ModelProviderStatus.ACTIVE,
            )
            timestamp = _now()
            profile: ModelProfile | None = None
            if initial_model:
                profile = ModelProfile(
                    id=(
                        "model-"
                        + hashlib.sha256(
                            (provider.id + selected_model).encode("utf-8")
                        ).hexdigest()[:24]
                    ),
                    org_id=effective_org_id(),
                    provider_id=provider.id,
                    model=selected_model,
                    display_name=f"{provider.display_name} · {selected_model}"[:160],
                    status=ModelProfileStatus.ACTIVE,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            with self._open_unit_of_work():
                self.providers.create(provider)
                if profile is not None:
                    self.model_profiles.create(profile)
                self.audit_events.create(
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
            self.secret_store.delete(secret_ref)
            raise
        return provider

    def refresh_provider_model_catalog(
        self, provider_id: str
    ) -> tuple[ModelProfile, ...]:
        """Delegate provider discovery and profile reconciliation."""
        return self.catalog_service.refresh_provider_model_catalog(provider_id)

    def update_model_profile(
        self,
        profile_id: str,
        *,
        default_temperature: float | None = None,
        max_output_tokens: int | None = None,
        cost_budget_usd: Decimal | None = None,
        input_cost_per_million_tokens_usd: Decimal | None = None,
        output_cost_per_million_tokens_usd: Decimal | None = None,
        status: ModelProfileStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> ModelProfile:
        """Update profile defaults used by newly published agent versions."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize_organization(
            requested_by_principal_id, Permission.MODELS_MANAGE
        )
        current = self.model_profiles.get_scoped(profile_id, effective_org_id())
        timestamp = _now()
        changes = {
            key: value
            for key, value in {
                "default_temperature": default_temperature,
                "max_output_tokens": max_output_tokens,
                "cost_budget_usd": cost_budget_usd,
                "input_cost_per_million_tokens_usd": input_cost_per_million_tokens_usd,
                "output_cost_per_million_tokens_usd": (
                    output_cost_per_million_tokens_usd
                ),
                "status": status,
                "updated_at": timestamp,
            }.items()
            if value is not None
        }
        updated = current.model_copy(update=changes)
        # Run strict validation after model_copy; copy intentionally skips it.
        updated = ModelProfile.model_validate(updated.model_dump())
        with self._open_unit_of_work():
            self.model_profiles.put(updated)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=current.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=requested_by_principal_id,
                    action="model_profile.updated",
                    target_type="model_profile",
                    target_id=profile_id,
                    summary="Model profile updated",
                    request_id=profile_id,
                    metadata={},
                    created_at=timestamp,
                )
            )
        return updated

    def rotate_model_provider_credential(
        self,
        provider_id: str,
        api_key: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> ModelProviderContract:
        """Replace a provider credential without returning or logging its value."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize_organization(
            requested_by_principal_id, Permission.MODELS_MANAGE
        )
        if not api_key.strip():
            raise ValueError("provider API key is required")
        provider = self.providers.get_scoped(provider_id, effective_org_id())
        next_secret_ref = self.secret_store.put(api_key.strip())
        old_secret_ref = provider.auth_ref
        updated: ModelProviderContract = provider.model_copy(
            update={"auth_ref": next_secret_ref}
        )
        updated = ModelProviderContract.model_validate(updated.model_dump())
        try:
            timestamp = _now()
            with self._open_unit_of_work():
                self.providers.put(updated)
                self.audit_events.create(
                    AuditEvent(
                        audit_event_id=_id(),
                        org_id=provider.org_id,
                        target_scope=AuditTargetScope.ORGANIZATION,
                        actor_principal_id=requested_by_principal_id,
                        action="model_provider.credential_rotated",
                        target_type="model_provider",
                        target_id=provider_id,
                        summary="Model provider credential rotated",
                        request_id=provider_id,
                        metadata={},
                        created_at=timestamp,
                    )
                )
        except Exception:
            self.secret_store.delete(next_secret_ref)
            raise
        if old_secret_ref:
            self.secret_store.delete(old_secret_ref)
        return updated
