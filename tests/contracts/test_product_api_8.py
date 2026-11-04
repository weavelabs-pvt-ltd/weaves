from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import ModelProviderStatus, ModelProviderType
from tests.support.runtime import LocalPlatformRuntime


def test_legacy_model_refresh_selects_a_configured_provider(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.model_service.create_model_provider(
        provider_type=ModelProviderType.ANTHROPIC,
        display_name="Anthropic test provider",
        api_key="anthropic-test-key",
        initial_model="claude-initial",
    )
    anthropic_adapter = runtime.model_adapters[ModelProviderType.ANTHROPIC]
    monkeypatch.setattr(
        anthropic_adapter,
        "list_models",
        lambda _provider: [{"id": "claude-discovered", "owned_by": "anthropic"}],
    )
    client = TestClient(create_app(runtime))

    with client:
        refreshed = client.post("/api/v0/models/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert [profile["model"] for profile in refreshed.json()] == [
            "claude-discovered"
        ]

        gemini = runtime.model_service.create_model_provider(
            provider_type=ModelProviderType.GEMINI,
            display_name="Gemini test provider",
            api_key="gemini-test-key",
            initial_model="gemini-initial",
        )
        gemini_adapter = runtime.model_adapters[ModelProviderType.GEMINI]
        monkeypatch.setattr(
            gemini_adapter,
            "list_models",
            lambda _provider: [{"id": "gemini-discovered", "owned_by": "google"}],
        )

        ambiguous = client.post("/api/v0/models/refresh")
        assert ambiguous.status_code == 422
        assert "provider_id is required" in ambiguous.json()["detail"]

        selected = client.post(
            "/api/v0/models/refresh", params={"provider_id": gemini.id}
        )
        assert selected.status_code == 200, selected.text
        assert [profile["model"] for profile in selected.json()] == [
            "gemini-discovered"
        ]

        disabled_provider = runtime.providers.get(gemini.id)
        runtime.providers.put(
            disabled_provider.model_copy(
                update={"status": ModelProviderStatus.DISABLED}
            )
        )
        disabled_refresh = client.post(
            "/api/v0/models/refresh", params={"provider_id": gemini.id}
        )
        assert disabled_refresh.status_code == 409
        assert disabled_refresh.json()["detail"] == "Model provider is disabled"
