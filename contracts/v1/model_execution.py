"""Provider-neutral model gateway request and response contracts."""

from pydantic import Field, model_validator

from contracts.v1.base import FrozenProductContract, OpaqueId, StrEnum


class ModelMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelMessage(FrozenProductContract):
    role: ModelMessageRole
    content: str = Field(min_length=1, max_length=100_000)


class ModelRequest(FrozenProductContract):
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=2048, ge=1, le=32768)
    request_id: OpaqueId | None = None


class ModelUsage(FrozenProductContract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def total_matches_components(self) -> "ModelUsage":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        return self


class ModelResponse(FrozenProductContract):
    provider_id: OpaqueId
    model_id: OpaqueId
    content: str = Field(min_length=1, max_length=100_000)
    usage: ModelUsage
    finish_reason: str | None = Field(default=None, max_length=64)
    upstream_request_id: OpaqueId | None = None


__all__ = [
    "ModelMessage",
    "ModelMessageRole",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
]
