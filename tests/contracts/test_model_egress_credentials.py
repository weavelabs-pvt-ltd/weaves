"""Denied model endpoints cannot leave new secrets or replace a working key."""

import httpx
import pytest

from contracts.v1 import ModelProviderType
from shared.errors import ModelGatewayError
from tests.support.runtime import LocalPlatformRuntime


def test_bad_registration_is_rejected_before_storing_credentials(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    before = runtime.providers.list()

    def reject_secret(_value):
        pytest.fail("Denied registration stored a credential")

    monkeypatch.setattr(runtime.secret_store, "put", reject_secret)
    try:
        with pytest.raises(ModelGatewayError) as error:
            runtime.model_service.create_model_provider(
                provider_type=ModelProviderType.ANTHROPIC,
                display_name="Rejected",
                api_key="fixture-only",
                base_url="https://169.254.169.254",
            )
        assert error.value.code == "model.egress_denied"
        assert runtime.providers.list() == before
    finally:
        runtime.close()


def test_legacy_rotation_denial_preserves_original_key_and_removes_new_secret(
    monkeypatch,
):
    runtime = LocalPlatformRuntime()
    adapter = runtime.model_adapters[ModelProviderType.OPENAI]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        )
    ) as mock:
        adapter._client = mock
        provider = runtime.model_service.create_model_provider(
            provider_type=ModelProviderType.OPENAI,
            display_name="Legacy",
            api_key="original-fixture-key",
            initial_model="fixture-model",
        )
    legacy = provider.model_copy(update={"base_url": "https://169.254.169.254/v1"})
    runtime.providers.put(legacy)
    references = []
    put = runtime.secret_store.put

    def capture(value, **kwargs):
        reference = put(value, **kwargs)
        references.append(reference)
        return reference

    monkeypatch.setattr(runtime.secret_store, "put", capture)
    try:
        with pytest.raises(ModelGatewayError) as error:
            runtime.model_service.rotate_model_provider_credential(
                provider.id, "replacement-fixture-key"
            )
        assert error.value.code == "model.egress_denied"
        assert runtime.providers.get(provider.id) == legacy
        assert runtime.secret_store.get(provider.auth_ref) == "original-fixture-key"
        assert len(references) == 1
        with pytest.raises(KeyError):
            runtime.secret_store.get(references[0])
    finally:
        runtime.close()
