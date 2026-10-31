"""Seed the local and environment-configured model providers and profiles."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TypeVar

from application.ports.repository import Repository
from contracts.v1 import (
    ModelProfile,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from contracts.v1.base import ProductContract
from modules.auth.runtime_organizations import MockModelIdentifier

TProduct = TypeVar("TProduct", bound=ProductContract)


class ModelBootstrapService:
    """Seed the mock model and optional environment-configured provider."""

    def __init__(
        self,
        *,
        providers: Repository[ModelProviderContract],
        profiles: Repository[ModelProfile],
        mock_model: MockModelIdentifier,
        default_profile_id: str,
        environment_enabled: bool,
        environment_provider_type: ModelProviderType,
        environment_model_name: str,
        environment_base_url: str | None,
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind model repositories and resolved process model settings."""
        self.providers = providers
        self.profiles = profiles
        self.mock_model = mock_model
        self.default_profile_id = default_profile_id
        self.environment_enabled = environment_enabled
        self.environment_provider_type = environment_provider_type
        self.environment_model_name = environment_model_name
        self.environment_base_url = environment_base_url
        self._open_unit_of_work = unit_of_work

    @staticmethod
    def _seed(
        repository: Repository[TProduct], id_field: str, record: TProduct
    ) -> TProduct:
        """Create a deterministic starter record only when it is absent."""
        record_id = str(getattr(record, id_field))
        try:
            return repository.get(record_id)
        except KeyError:
            try:
                return repository.create(record)
            except ValueError:
                return repository.get(record_id)

    def seed(self, now: datetime) -> None:
        """Seed the local fallback and configured model profiles."""
        self._seed_local_model(now)
        if self.environment_enabled:
            self._seed_environment_model(now)

    def repair_profile_allowlists(self) -> None:
        """Restore provider model allowlists referenced by persisted profiles."""
        profiles_by_provider: dict[str, set[str]] = {}
        for profile in self.profiles.list():
            profiles_by_provider.setdefault(profile.provider_id, set()).add(
                profile.model
            )
        repairs = self._missing_provider_models(profiles_by_provider)
        if not repairs:
            return
        with self._open_unit_of_work():
            for provider in repairs:
                self.providers.put(provider)

    def _missing_provider_models(
        self, profiles_by_provider: dict[str, set[str]]
    ) -> list[ModelProviderContract]:
        """Build provider records that need referenced model names restored."""
        repairs: list[ModelProviderContract] = []
        for provider in self.providers.list():
            missing = sorted(
                profiles_by_provider.get(provider.id, set()).difference(
                    provider.allowed_models
                )
            )
            if missing:
                repaired = provider.model_copy(
                    update={"allowed_models": [*provider.allowed_models, *missing]}
                )
                repairs.append(
                    ModelProviderContract.model_validate(repaired.model_dump())
                )
        return repairs

    def _seed_local_model(self, now: datetime) -> None:
        """Ensure the deterministic mock provider and profile exist."""
        self._seed(
            self.providers,
            "id",
            ModelProviderContract(
                id=self.mock_model.provider_id,
                org_id="local-org",
                provider_type=ModelProviderType.LOCAL,
                display_name="Deterministic mock",
                allowed_models=[self.mock_model.model_id],
                status=ModelProviderStatus.ACTIVE,
            ),
        )
        self._seed(
            self.profiles,
            "id",
            ModelProfile(
                id="local-model-profile",
                org_id="local-org",
                provider_id=self.mock_model.provider_id,
                model=self.mock_model.model_id,
                display_name="Local mock model",
                status=ModelProfileStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )

    def _seed_environment_model(self, now: datetime) -> None:
        """Persist the model provider configured through process settings."""
        provider_id = (
            "openai-provider"
            if self.environment_provider_type is ModelProviderType.OPENAI
            else "configured-model-provider"
        )
        self._seed(
            self.providers,
            "id",
            ModelProviderContract(
                id=provider_id,
                org_id="local-org",
                provider_type=self.environment_provider_type,
                display_name=self.environment_provider_type.value.replace(
                    "_", " "
                ).title(),
                allowed_models=[self.environment_model_name],
                auth_ref="env:MODEL_API_KEY",
                base_url=self.environment_base_url,
                status=ModelProviderStatus.ACTIVE,
            ),
        )
        self._seed(
            self.profiles,
            "id",
            ModelProfile(
                id=self.default_profile_id,
                org_id="local-org",
                provider_id=provider_id,
                model=self.environment_model_name,
                display_name=(
                    f"{self.environment_provider_type.value.title()} · "
                    f"{self.environment_model_name}"
                ),
                status=ModelProfileStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
