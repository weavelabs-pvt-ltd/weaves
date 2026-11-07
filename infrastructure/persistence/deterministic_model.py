"""Deterministic model and context providers used by local development."""

from clients.model_providers.gateway import ModelAdapter
from contracts.v1 import ModelProfileBinding
from contracts.v1.model_execution import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class DeterministicMockModel(ModelAdapter):
    """Produces repeatable output without network access or model credentials."""

    provider_id = "local-mock-provider"
    model_id = "weaves-deterministic-v1"

    def complete(
        self, request: ModelRequest, binding: ModelProfileBinding
    ) -> ModelResponse:
        """Return a stable response for local runs without a provider call."""
        prompt = request.messages[-1].content
        response_text = f"Local assistant response:\n\n{prompt}"
        input_tokens = max(
            1, sum(len(message.content.split()) for message in request.messages)
        )
        output_tokens = max(1, len(response_text.split()))
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content=response_text,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason="stop",
        )
