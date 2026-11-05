"""Model-provider and model-profile lifecycle use cases."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    ModelAccessMode,
    ModelProfile,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
    OrganizationModelConfiguration,
    Permission,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.models.catalog import ModelCatalogService, model_card_metadata
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import ModelGatewayError
from shared.request_context import effective_org_id, effective_principal_id


class ModelService:
    """Manage model providers and profiles through explicit capability ports."""

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        model_profiles: Repository[ModelProfile],
        model_configurations: Repository[OrganizationModelConfiguration],
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
        self.model_configurations = model_configurations
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
        configuration = self._configuration_or_none()
        if configuration and configuration.mode is ModelAccessMode.CUSTOMER_MANAGED:
            profiles = tuple(
                profile
                for profile in profiles
                if self._is_customer_managed_provider(
                    self.providers.get_scoped(profile.provider_id, effective_org_id())
                )
            )
        enriched_profiles: list[ModelProfile] = []
        for profile in profiles:
            provider = self.providers.get_scoped(
                profile.provider_id, effective_org_id()
            )
            metadata = model_card_metadata(provider.provider_type, profile.model)
            updates = {
                key: value for key, value in metadata.items() if value is not None
            }
            enriched_profiles.append(profile.model_copy(update=updates))
        profiles = tuple(enriched_profiles)
        default_profile_id = (
            configuration.default_model_profile_id
            if configuration and configuration.default_model_profile_id
            else self.default_model_profile_id
        )
        return tuple(sorted(profiles, key=lambda item: item.id != default_profile_id))

    def list_model_providers(self) -> tuple[ModelProviderContract, ...]:
        """List organization providers without exposing secret references."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize(
            effective_principal_id("local-developer"), Permission.MODELS_VIEW
        )
        providers = self.providers.list_scoped(effective_org_id())
        configuration = self._configuration_or_none()
        if configuration and configuration.mode is ModelAccessMode.CUSTOMER_MANAGED:
            providers = tuple(
                provider
                for provider in providers
                if self._is_customer_managed_provider(provider)
            )
        return providers

    def get_model_configuration(self) -> OrganizationModelConfiguration | None:
        """Return the active organization's selected model ownership mode."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize(
            effective_principal_id("local-developer"), Permission.MODELS_VIEW
        )
        try:
            return self.model_configurations.get_scoped(
                effective_org_id(), effective_org_id()
            )
        except KeyError:
            return None

    def configure_model_mode(
        self,
        mode: ModelAccessMode,
        *,
        provider_id: str | None = None,
        model_profile_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> OrganizationModelConfiguration:
        """Select a supported organization model mode and optional default model."""
        if not self._is_bootstrapped():
            self._bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self._authorize_organization(actor, Permission.MODELS_MANAGE)
        if mode is ModelAccessMode.WEAVES_MANAGED:
            raise ModelGatewayError(
                "model.mode_unavailable",
                "Weaves-managed model routing is not available yet",
            )
        current = self._configuration_or_none()
        if current and current.mode not in {None, mode}:
            raise ValueError(
                "changing model access mode requires an explicit migration"
            )
        selected_profile_id = model_profile_id or (
            current.default_model_profile_id if current else None
        )
        selected_provider: ModelProviderContract | None = None
        if selected_profile_id:
            profile = self.model_profiles.get_scoped(
                selected_profile_id, effective_org_id()
            )
            provider_id = provider_id or profile.provider_id
            selected_provider = self.providers.get_scoped(
                provider_id, effective_org_id()
            )
            if profile.provider_id != selected_provider.id:
                raise ValueError("default model profile does not belong to provider")
            if profile.status is not ModelProfileStatus.ACTIVE:
                raise ValueError("default model profile must be active")
        elif provider_id:
            selected_provider = self.providers.get_scoped(
                provider_id, effective_org_id()
            )
        elif current and current.default_provider_id:
            provider_id = current.default_provider_id
            selected_provider = self.providers.get_scoped(
                provider_id, effective_org_id()
            )
        if selected_provider is None:
            raise ValueError(
                "configure an organization provider before selecting this mode"
            )
        if not self._is_customer_managed_provider(selected_provider):
            raise ValueError(
                "customer-managed mode requires an organization-owned provider key"
            )
        if selected_provider.status is not ModelProviderStatus.ACTIVE:
            raise ValueError("default model provider must be active")
        config = OrganizationModelConfiguration(
            org_id=effective_org_id(),
            mode=mode,
            default_provider_id=provider_id,
            default_model_profile_id=selected_profile_id,
            created_at=current.created_at if current else _now(),
            updated_at=_now(),
        )
        with self._open_unit_of_work():
            self.model_configurations.put(config)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=config.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=actor,
                    action="model_configuration.updated",
                    target_type="model_configuration",
                    target_id=config.org_id,
                    summary="Organization model configuration updated",
                    request_id=config.org_id,
                    metadata={"mode": mode.value},
                    created_at=config.updated_at,
                )
            )
        return config

    def validate_provider_credential(
        self,
        *,
        provider_type: ModelProviderType,
        api_key: str,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[dict[str, str], ...]:
        """Check an OpenAI or Anthropic key without persisting the connection."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize_organization(
            effective_principal_id(requested_by_principal_id), Permission.MODELS_MANAGE
        )
        if provider_type not in {
            ModelProviderType.OPENAI,
            ModelProviderType.ANTHROPIC,
        }:
            raise ValueError("only OpenAI and Anthropic credentials are supported")
        if not api_key.strip():
            raise ValueError("provider API key is required")
        secret_ref = self.secret_store.put(api_key.strip())
        base_urls = {
            ModelProviderType.OPENAI: "https://api.openai.com/v1",
            ModelProviderType.ANTHROPIC: "https://api.anthropic.com",
        }
        transient_provider = ModelProviderContract(
            id=_id(),
            org_id=effective_org_id(),
            provider_type=provider_type,
            display_name="Credential validation",
            allowed_models=["validation-pending"],
            auth_ref=secret_ref,
            base_url=base_urls[provider_type],
            status=ModelProviderStatus.ACTIVE,
        )
        try:
            models = self.catalog_service.preview_provider_models(transient_provider)
            return tuple(item for item in models if isinstance(item.get("id"), str))
        finally:
            self.secret_store.delete(secret_ref)

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
        current_configuration = self._configuration_or_none()
        if (
            current_configuration
            and current_configuration.mode is ModelAccessMode.WEAVES_MANAGED
        ):
            raise ValueError(
                "customer provider keys are disabled in Weaves-managed mode"
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
        candidate = ModelProviderContract(
            id=provider_id,
            org_id=effective_org_id(),
            provider_type=provider_type,
            display_name=display_name,
            allowed_models=[selected_model],
            auth_ref=secret_ref,
            base_url=(base_url.strip() if base_url else defaults[provider_type]),
            status=ModelProviderStatus.ACTIVE,
        )
        try:
            discovered_models = self.catalog_service.preview_provider_models(candidate)
            discovered_ids = {item["id"] for item in discovered_models}
            if initial_model and selected_model not in discovered_ids:
                raise ValueError("selected model is not available to this API key")
        except Exception:
            self.secret_store.delete(secret_ref)
            raise
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
            api_key.strip()[-4:] if len(api_key.strip()) >= 4 else None,
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
            profile = self._build_initial_profile(
                provider, initial_model, selected_model, timestamp
            )
            with self._open_unit_of_work():
                self.providers.create(provider)
                if profile is not None:
                    self.model_profiles.create(profile)
                existing_config = self._configuration_or_none()
                config = OrganizationModelConfiguration(
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
                    created_at=(
                        existing_config.created_at if existing_config else timestamp
                    ),
                    updated_at=timestamp,
                )
                self.model_configurations.put(config)
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

    @staticmethod
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

    def _configuration_or_none(self) -> OrganizationModelConfiguration | None:
        """Load the current organization model settings when already configured."""
        try:
            return self.model_configurations.get_scoped(
                effective_org_id(), effective_org_id()
            )
        except KeyError:
            return None

    @staticmethod
    def _is_customer_managed_provider(provider: ModelProviderContract) -> bool:
        """Return whether provider credentials are owned by the organization."""
        return (
            provider.provider_type is not ModelProviderType.LOCAL
            and bool(provider.auth_ref)
            and not provider.auth_ref.startswith("env:")
        )

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
        configuration = self._configuration_or_none()
        if (
            status is ModelProfileStatus.DISABLED
            and configuration
            and configuration.default_model_profile_id == profile_id
        ):
            raise ValueError("select another default model before disabling this one")
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
            update={
                "auth_ref": next_secret_ref,
                "credential_last4": api_key.strip()[-4:]
                if len(api_key.strip()) >= 4
                else None,
            }
        )
        updated = ModelProviderContract.model_validate(updated.model_dump())
        try:
            self.catalog_service.preview_provider_models(updated)
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
