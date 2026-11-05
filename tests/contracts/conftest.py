"""Keep setup credentials in selected legacy tests away from real providers."""

import pytest

from clients.model_providers.anthropic import AnthropicAdapter
from clients.model_providers.gemini import GeminiAdapter
from clients.model_providers.openai_compatible import OpenAICompatibleAdapter


@pytest.fixture(autouse=True)
def catalog_boundary_for_legacy_setup(request, monkeypatch):
    """Old setup fixtures now validate keys before persistence; stub that boundary."""
    modules = {
        "test_rbac_api",
        "test_rbac_api_workspace_scope",
        "test_product_api_4",
        "test_product_api_8",
    }
    gateway_setups = {
        "test_model_provider_and_profile_writes_roll_back_with_failed_audit",
        "test_model_catalog_refresh_writes_profiles_and_audit_atomically",
        "test_disabled_provider_catalog_refresh_does_not_call_adapter",
    }
    module = request.module.__name__.rsplit(".", 1)[-1]
    if module not in modules and request.node.name not in gateway_setups:
        return

    def list_models(self, provider):
        return [{"id": model} for model in provider.allowed_models]

    monkeypatch.setattr(OpenAICompatibleAdapter, "list_models", list_models)
    monkeypatch.setattr(AnthropicAdapter, "list_models", list_models)
    monkeypatch.setattr(GeminiAdapter, "list_models", list_models)
