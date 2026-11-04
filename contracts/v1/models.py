from decimal import Decimal
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator, model_validator

from contracts.v1.base import (
    DisplayName,
    MutableOrgScopedContract,
    OpaqueId,
    OrgScopedContract,
    ProductContract,
    StrEnum,
)
from contracts.v1.model_configuration import ModelAccessMode

ModelName = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")
]


class ModelProviderType(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    OPENAI_COMPATIBLE = "openai_compatible"
    LOCAL = "local"


class ModelProviderStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ModelProfileStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ModelProvider(OrgScopedContract):
    id: OpaqueId
    provider_type: ModelProviderType
    display_name: DisplayName
    allowed_models: list[ModelName] = Field(min_length=1, max_length=500)
    auth_ref: OpaqueId | None = None
    base_url: str | None = Field(default=None, min_length=8, max_length=2048)
    status: ModelProviderStatus

    @field_validator("base_url")
    @classmethod
    def base_url_must_be_http(
        cls: type["ModelProvider"], value: str | None
    ) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        sensitive_query_keys = {
            "access_token",
            "api_key",
            "apikey",
            "password",
            "secret",
            "token",
        }
        if (
            parsed.username
            or parsed.password
            or any(
                key.lower() in sensitive_query_keys
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        ):
            raise ValueError("base_url must not contain credentials")
        return value

    @model_validator(mode="after")
    def allowed_models_are_unique(self: "ModelProvider") -> "ModelProvider":
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must be unique")
        return self


class ModelProfile(MutableOrgScopedContract):
    id: OpaqueId
    provider_id: OpaqueId
    model: ModelName
    display_name: DisplayName
    default_temperature: Annotated[float, Field(ge=0.0, le=2.0)] = 0.2
    max_output_tokens: Annotated[int, Field(ge=1, le=32768)] = 2048
    cost_budget_usd: (
        Annotated[Decimal, Field(gt=Decimal("0"), le=Decimal("100000"))] | None
    ) = None
    input_cost_per_million_tokens_usd: (
        Annotated[
            Decimal,
            Field(
                ge=Decimal("0"), le=Decimal("100000"), max_digits=18, decimal_places=9
            ),
        ]
        | None
    ) = None
    output_cost_per_million_tokens_usd: (
        Annotated[
            Decimal,
            Field(
                ge=Decimal("0"), le=Decimal("100000"), max_digits=18, decimal_places=9
            ),
        ]
        | None
    ) = None
    fallback_model_profile_id: OpaqueId | None = None
    status: ModelProfileStatus

    @model_validator(mode="after")
    def fallback_must_not_be_self(self: "ModelProfile") -> "ModelProfile":
        if self.fallback_model_profile_id == self.id:
            raise ValueError("fallback model profile cannot reference itself")
        return self


class ModelProfileBinding(ProductContract):
    """A resolved profile/provider pair with cross-reference scope checked."""

    provider: ModelProvider
    profile: ModelProfile
    access_mode: ModelAccessMode | None = None

    @model_validator(mode="after")
    def provider_and_profile_must_match(
        self: "ModelProfileBinding",
    ) -> "ModelProfileBinding":
        if self.profile.org_id != self.provider.org_id:
            raise ValueError("model profile and provider must share org_id")
        if self.profile.provider_id != self.provider.id:
            raise ValueError("model profile must reference the supplied provider")
        if self.profile.model not in self.provider.allowed_models:
            raise ValueError("model profile model must be allowed by its provider")
        if self.access_mode is ModelAccessMode.CUSTOMER_MANAGED and (
            self.provider.provider_type is ModelProviderType.LOCAL
            or (self.provider.auth_ref or "").startswith("env:")
        ):
            raise ValueError(
                "customer-managed mode requires an organization-owned provider key"
            )
        return self


__all__ = [
    "ModelProfile",
    "ModelProfileBinding",
    "ModelProfileStatus",
    "ModelProvider",
    "ModelProviderStatus",
    "ModelProviderType",
]
