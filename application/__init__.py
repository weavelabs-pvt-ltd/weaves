"""Application package with lazy exports for legacy public imports."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AgentRuntimeBudgetExceeded": (
        "application.platform",
        "AgentRuntimeBudgetExceeded",
    ),
    "AnthropicAdapter": ("clients.model_providers.gateway", "AnthropicAdapter"),
    "EnvironmentSecretResolver": (
        "clients.model_providers.gateway",
        "EnvironmentSecretResolver",
    ),
    "GeminiAdapter": ("clients.model_providers.gateway", "GeminiAdapter"),
    "IdempotencyConflict": ("shared.errors", "IdempotencyConflict"),
    "LocalPlatformRuntime": ("application.platform", "LocalPlatformRuntime"),
    "ModelGateway": ("clients.model_providers.gateway", "ModelGateway"),
    "ModelGatewayError": ("clients.model_providers.gateway", "ModelGatewayError"),
    "OpenAICompatibleAdapter": (
        "clients.model_providers.gateway",
        "OpenAICompatibleAdapter",
    ),
    "PluginGatewayError": ("shared.errors", "PluginGatewayError"),
    "RunCostBudgetExceeded": ("application.platform", "RunCostBudgetExceeded"),
    "RuntimeResult": ("application.platform", "RuntimeResult"),
    "SecretResolver": ("clients.model_providers.gateway", "SecretResolver"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load a compatibility export only when a caller requests it."""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module_name), attribute)
