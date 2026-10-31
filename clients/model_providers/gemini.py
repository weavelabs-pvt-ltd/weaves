"""Adapter for Google's Gemini generateContent API."""

from __future__ import annotations

import httpx

from clients.model_providers.gateway_base import SecretResolver
from clients.model_providers.gateway_http import raise_upstream_error
from contracts.v1 import (
    ModelProfileBinding,
    ModelProvider,
)
from contracts.v1.model_execution import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from shared.errors import ModelGatewayError


class GeminiAdapter:
    """Gemini generateContent adapter using API-key authentication headers."""

    def __init__(
        self,
        secret_resolver: SecretResolver,
        client: httpx.Client,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Create a Gemini adapter using a shared HTTP client."""
        self._secret_resolver = secret_resolver
        self._client = client
        self._timeout_seconds = timeout_seconds

    def list_models(self, provider: ModelProvider) -> tuple[dict[str, str], ...]:
        """List Gemini models available to the configured credential."""
        secret = self._resolve_secret(provider.auth_ref)
        try:
            response = self._client.get(
                f"{(provider.base_url or 'https://generativelanguage.googleapis.com/v1beta').rstrip('/')}/models",
                headers={"x-goog-api-key": secret},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "model.transport_error", "Model provider request failed", retryable=True
            ) from exc
        if response.is_error:
            raise_upstream_error(response)
        try:
            models = []
            for item in response.json().get("models", []):
                name = item.get("name")
                methods = item.get("supportedGenerationMethods", [])
                if isinstance(name, str) and "generateContent" in methods:
                    models.append(
                        {"id": name.removeprefix("models/"), "owned_by": "google"}
                    )
            return tuple(sorted(models, key=lambda model: model["id"]))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "model.invalid_response", "Model provider returned an invalid response"
            ) from exc

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        """Generate a response through Gemini's generateContent API."""
        secret = self._resolve_secret(binding.provider.auth_ref)
        contents = [
            {
                "role": "model" if message.role.value == "assistant" else "user",
                "parts": [{"text": message.content}],
            }
            for message in request.messages
            if message.role.value != "system"
        ]
        system = "\n\n".join(
            message.content
            for message in request.messages
            if message.role.value == "system"
        )
        body: dict[str, object] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        model = binding.profile.model.removeprefix("models/")
        try:
            response = self._client.post(
                f"{(binding.provider.base_url or 'https://generativelanguage.googleapis.com/v1beta').rstrip('/')}/models/{model}:generateContent",
                json=body,
                headers={"x-goog-api-key": secret},
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
            payload = response.json()
            candidates = payload["candidates"]
            content = "".join(
                part.get("text", "")
                for part in candidates[0]["content"]["parts"]
                if isinstance(part, dict)
            )
            usage = payload.get("usageMetadata", {})
            input_tokens = int(usage.get("promptTokenCount", 0))
            output_tokens = int(usage.get("candidatesTokenCount", 0))
            finish_reason = candidates[0].get("finishReason")
        except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "model.invalid_response", "Model provider returned an invalid response"
            ) from exc
        if not content:
            raise ModelGatewayError(
                "model.empty_response", "Model provider returned no text content"
            )
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content=content,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            upstream_request_id=response.headers.get("x-request-id"),
        )

    def _resolve_secret(self, auth_ref: str | None) -> str:
        if not auth_ref:
            raise ModelGatewayError(
                "model.auth_ref_missing",
                "Provider has no configured credential reference",
            )
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
