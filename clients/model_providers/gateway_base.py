"""Provider-neutral model gateway ports and environment secret lookup."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Protocol

from pydantic import ValidationError

from contracts.v1 import (
    ModelProfileBinding,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
)
from contracts.v1.model_execution import ModelRequest, ModelResponse
from shared.errors import ModelGatewayError as ModelGatewayError
from shared.metrics import MODEL_CALL_SECONDS, MODEL_CALLS, USAGE_TOKENS


def _observe_call(started: float, outcome: str) -> None:
    """Record one provider call's duration and outcome (no model or tenant label)."""
    MODEL_CALL_SECONDS.observe(time.perf_counter() - started)
    MODEL_CALLS.inc(outcome=outcome)


class SecretResolver(Protocol):
    """Resolve opaque secret references without exposing storage details."""

    def resolve(self, auth_ref: str) -> str:
        """Return the credential material for an opaque reference."""
        ...


class EnvironmentSecretResolver:
    """Resolve the one explicitly supported local-development secret reference."""

    def resolve(self, auth_ref: str) -> str:
        """Resolve only the local development model-key reference."""
        if auth_ref != "env:MODEL_API_KEY":
            raise KeyError("unsupported environment secret reference")
        return os.environ.get("MODEL_API_KEY", "")


class ModelAdapter(Protocol):
    """Provider-specific implementation of model completion."""

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        """Generate a response for one validated model binding."""
        ...


class ModelGateway:
    """Routes a provider-neutral request through the selected provider adapter."""

    def __init__(self, adapters: Mapping[ModelProviderType, ModelAdapter]) -> None:
        """Create a router from the supported provider adapters."""
        self._adapters = dict(adapters)

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        """Validate the binding and route a neutral request to its adapter."""
        provider = binding.provider
        profile = binding.profile
        if profile.model not in provider.allowed_models:
            raise ModelGatewayError(
                "model.not_allowed", "Model is not allowed by provider"
            )
        if (
            profile.status is not ModelProfileStatus.ACTIVE
            or provider.status is not ModelProviderStatus.ACTIVE
        ):
            raise ModelGatewayError(
                "model.disabled", "Model profile or provider is disabled"
            )
        try:
            adapter = self._adapters[provider.provider_type]
        except KeyError as exc:
            raise ModelGatewayError(
                "model.adapter_missing",
                "No adapter is registered for the selected provider",
            ) from exc
        started = time.perf_counter()
        try:
            response = adapter.complete(request, binding)
        except ValidationError as exc:
            _observe_call(started, "invalid_response")
            raise ModelGatewayError(
                "model.invalid_response",
                "Model provider returned a response that violates "
                "the platform contract",
            ) from exc
        except Exception:
            _observe_call(started, "error")
            raise
        _observe_call(started, "ok")
        USAGE_TOKENS.inc(response.usage.input_tokens, direction="input")
        USAGE_TOKENS.inc(response.usage.output_tokens, direction="output")
        if response.provider_id != provider.id:
            raise ModelGatewayError(
                "model.provider_mismatch",
                "Model adapter response did not match the selected provider",
            )
        return response
