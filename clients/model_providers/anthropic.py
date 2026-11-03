"""Adapter for Anthropic Messages API model completions."""

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


class AnthropicAdapter:
    """Messages API adapter for Anthropic API-key credentials."""

    def __init__(
        self,
        secret_resolver: SecretResolver,
        client: httpx.Client,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Create an Anthropic adapter using a shared HTTP client."""
        self._secret_resolver = secret_resolver
        self._client = client
        self._timeout_seconds = timeout_seconds

    def list_models(self, provider: ModelProvider) -> tuple[dict[str, str], ...]:
        """List Anthropic models available to the configured credential."""
        secret = self._resolve_secret(provider.auth_ref)
        try:
            response = self._client.get(
                f"{(provider.base_url or 'https://api.anthropic.com').rstrip('/')}/v1/models",
                headers={"x-api-key": secret, "anthropic-version": "2023-06-01"},
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "model.transport_error", "Model provider request failed", retryable=True
            ) from exc
        if response.is_error:
            raise_upstream_error(response)
        try:
            data = response.json()["data"]
            return tuple(
                sorted(
                    (
                        {"id": item["id"], "owned_by": "anthropic"}
                        for item in data
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                        and item["id"]
                    ),
                    key=lambda model: model["id"],
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "model.invalid_response", "Model provider returned an invalid response"
            ) from exc

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        """Generate a response through Anthropic's Messages API."""
        secret = self._resolve_secret(binding.provider.auth_ref)
        system = "\n\n".join(
            message.content
            for message in request.messages
            if message.role.value == "system"
        )
        messages = [
            {"role": message.role.value, "content": message.content}
            for message in request.messages
            if message.role.value != "system"
        ]
        body: dict[str, object] = {
            "model": binding.profile.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }
        if not _uses_default_sampling_parameters(binding.profile.model):
            body["temperature"] = request.temperature
        if system:
            body["system"] = system
        try:
            response = self._client.post(
                f"{(binding.provider.base_url or 'https://api.anthropic.com').rstrip('/')}/v1/messages",
                json=body,
                headers={"x-api-key": secret, "anthropic-version": "2023-06-01"},
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
            content = "".join(
                item.get("text", "")
                for item in payload["content"]
                if isinstance(item, dict) and item.get("type") == "text"
            )
            usage = payload.get("usage", {})
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
            model_id = payload.get("model", binding.profile.model)
            finish_reason = payload.get("stop_reason")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ModelGatewayError(
                "model.invalid_response", "Model provider returned an invalid response"
            ) from exc
        if not isinstance(content, str) or not content:
            raise ModelGatewayError(
                "model.empty_response", "Model provider returned no text content"
            )
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=model_id if isinstance(model_id, str) else binding.profile.model,
            content=content,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            upstream_request_id=response.headers.get("request-id"),
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


def _uses_default_sampling_parameters(model: str) -> bool:
    """Return whether Anthropic requires default sampling settings for a model.

    Anthropic rejects non-default temperature for Claude 4.7+ and newer model
    families. The adapter omits temperature for those models so configured
    profile values cannot make otherwise valid requests fail with HTTP 400.
    """
    normalized = model.lower().removeprefix("claude-")
    if "mythos-preview" in normalized:
        return True

    family, _, version = normalized.partition("-")
    version_parts = version.split("-")
    if not family.isalpha() or not version_parts or not version_parts[0].isdigit():
        return False

    major = int(version_parts[0])
    if major >= 5:
        return True
    if major == 4 and family == "opus" and len(version_parts) > 1:
        minor = version_parts[1]
        return minor.isdigit() and int(minor) >= 7
    return False
