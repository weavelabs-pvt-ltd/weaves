"""OpenAI-compatible text completion adapter."""

from __future__ import annotations

import httpx

from clients.model_providers.gateway_base import SecretResolver
from clients.model_providers.gateway_http import raise_upstream_error
from contracts.v1 import (
    ModelProfileBinding,
    ModelProvider,
    ModelProviderType,
)
from contracts.v1.model_execution import ModelRequest, ModelResponse, ModelUsage
from shared.errors import ModelGatewayError


class OpenAICompatibleAdapter:
    """Chat-completions adapter that resolves credentials only at request time."""

    def __init__(
        self,
        secret_resolver: SecretResolver,
        client: httpx.Client,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Create the adapter over a reusable HTTP client and secret resolver."""
        self._secret_resolver = secret_resolver
        self._client = client
        self._timeout_seconds = timeout_seconds

    def list_models(self, provider: ModelProvider) -> tuple[dict[str, str], ...]:
        """Return models visible to a provider credential, without prompt usage."""
        if provider.provider_type not in {
            ModelProviderType.OPENAI,
            ModelProviderType.OPENAI_COMPATIBLE,
        }:
            raise ModelGatewayError(
                "model.provider_unsupported",
                "Provider is not supported by this adapter",
            )
        if not provider.auth_ref:
            raise ModelGatewayError(
                "model.auth_ref_missing",
                "Provider has no configured credential reference",
            )
        base_url = provider.base_url or "https://api.openai.com/v1"
        secret = self._resolve_secret(provider.auth_ref)
        try:
            response = self._client.get(
                f"{base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelGatewayError(
                "model.timeout", "Model provider request timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "model.transport_error", "Model provider request failed", retryable=True
            ) from exc
        if response.is_error:
            raise_upstream_error(response)
        try:
            data = response.json()["data"]
            models = tuple(
                {"id": item["id"], "owned_by": item.get("owned_by", "unknown")}
                for item in data
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"]
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "model.invalid_response", "Model provider returned an invalid response"
            ) from exc
        return tuple(sorted(models, key=lambda model: model["id"]))

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        """Send one provider-neutral completion request."""
        provider = binding.provider
        self._ensure_supported_provider(provider.provider_type)
        if not provider.auth_ref:
            raise ModelGatewayError(
                "model.auth_ref_missing",
                "Provider has no configured credential reference",
            )
        base_url = provider.base_url or "https://api.openai.com/v1"
        secret = self._resolve_secret(provider.auth_ref)

        body = {
            "model": binding.profile.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        try:
            response = self._client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {secret}"},
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelGatewayError(
                "model.timeout", "Model provider request timed out", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "model.transport_error", "Model provider request failed", retryable=True
            ) from exc

        if response.is_error:
            raise_upstream_error(response)
        return self._parse_response(response, binding, provider.id)

    @staticmethod
    def _ensure_supported_provider(provider_type: ModelProviderType) -> None:
        """Reject providers that use an incompatible API."""
        if provider_type not in {
            ModelProviderType.OPENAI,
            ModelProviderType.OPENAI_COMPATIBLE,
        }:
            raise ModelGatewayError(
                "model.provider_unsupported",
                "Provider is not supported by this adapter",
            )

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        binding: ModelProfileBinding,
        provider_id: str,
    ) -> ModelResponse:
        """Validate and normalize completion text, model, and token usage."""
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
            model_id = payload.get("model", binding.profile.model)
            finish_reason = payload["choices"][0].get("finish_reason")
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "model.invalid_response", "Model provider returned an invalid response"
            ) from exc
        if not isinstance(content, str) or not content:
            raise ModelGatewayError(
                "model.empty_response", "Model provider returned no text content"
            )
        if not isinstance(model_id, str) or not 1 <= len(model_id.strip()) <= 128:
            model_id = binding.profile.model
        else:
            model_id = model_id.strip()
        request_id = response.headers.get("x-request-id")
        if request_id is not None and not 1 <= len(request_id.strip()) <= 128:
            request_id = None
        elif request_id is not None:
            request_id = request_id.strip()
        return ModelResponse(
            provider_id=provider_id,
            model_id=model_id,
            content=content,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason=finish_reason,
            upstream_request_id=request_id,
        )

    def _resolve_secret(self, auth_ref: str) -> str:
        try:
            secret = self._secret_resolver.resolve(auth_ref)
        except Exception as exc:
            raise ModelGatewayError(
                "model.credential_unavailable",
                "Provider credential could not be resolved",
            ) from exc
        if not secret:
            raise ModelGatewayError(
                "model.credential_unavailable",
                "Provider credential could not be resolved",
            )
        return secret
