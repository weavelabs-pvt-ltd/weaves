import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from clients.model_providers.gateway import (
    ModelGateway,
    ModelGatewayError,
    OpenAICompatibleAdapter,
)
from contracts.v1 import (
    ModelMessageRole,
    ModelProfile,
    ModelProfileBinding,
    ModelProfileStatus,
    ModelProvider,
    ModelProviderStatus,
    ModelProviderType,
    ModelRequest,
    ModelResponse,
    RunStatus,
)
from contracts.v1.model_execution import ModelUsage
from tests.support.runtime import LocalPlatformRuntime


def binding(**provider_overrides):
    provider = ModelProvider(
        id="provider-a",
        org_id="org-a",
        provider_type=ModelProviderType.OPENAI_COMPATIBLE,
        display_name="Compatible test provider",
        allowed_models=["model-a"],
        auth_ref="vault://provider-a",
        base_url="https://models.example/v1",
        status=ModelProviderStatus.ACTIVE,
        **provider_overrides,
    )
    profile = ModelProfile(
        id="profile-a",
        org_id="org-a",
        provider_id="provider-a",
        model="model-a",
        display_name="Test model",
        status=ModelProfileStatus.ACTIVE,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    return ModelProfileBinding(provider=provider, profile=profile)


def request():
    return ModelRequest(
        messages=(
            {"role": ModelMessageRole.SYSTEM, "content": "Answer concisely."},
            {"role": ModelMessageRole.USER, "content": "Summarize the context."},
        ),
        request_id="run-a",
    )


class TestSecretResolver:
    def resolve(self, auth_ref: str) -> str:
        assert auth_ref == "vault://provider-a"
        return "test-secret"


def test_openai_compatible_adapter_sends_profile_and_returns_sanitized_response():
    sent = {}

    def handler(http_request):
        sent["url"] = str(http_request.url)
        sent["headers"] = dict(http_request.headers)
        sent["body"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "upstream-request-a"},
            json={
                "model": "model-a-2026-01",
                "choices": [
                    {
                        "message": {"content": "A concise response."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(TestSecretResolver(), client)
    result = adapter.complete(request(), binding())

    assert sent["url"] == "https://models.example/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer test-secret"
    assert sent["body"]["model"] == "model-a"
    assert result.content == "A concise response."
    assert result.usage.total_tokens == 17
    assert result.model_id == "model-a-2026-01"
    assert "test-secret" not in result.model_dump_json()
    client.close()


def test_gateway_rejects_disabled_or_unapproved_model_profiles():
    compatible = binding()
    gateway = ModelGateway({})
    with pytest.raises(ModelGatewayError, match="No adapter"):
        gateway.complete(request(), compatible)
    with pytest.raises(ValidationError, match="must be allowed"):
        unapproved_provider = ModelProvider(
            id="provider-a",
            org_id="org-a",
            provider_type=ModelProviderType.OPENAI_COMPATIBLE,
            display_name="Compatible test provider",
            allowed_models=["different-model"],
            auth_ref="vault://provider-a",
            base_url="https://models.example/v1",
            status=ModelProviderStatus.ACTIVE,
        )
        ModelProfileBinding(provider=unapproved_provider, profile=compatible.profile)


def test_model_usage_totals_are_consistent():
    with pytest.raises(ValidationError, match="total_tokens"):
        ModelUsage(input_tokens=3, output_tokens=2, total_tokens=4)


def test_adapter_errors_do_not_expose_provider_response_bodies_or_secrets():
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(401, text="test-secret must not be copied")
        )
    )
    adapter = OpenAICompatibleAdapter(TestSecretResolver(), client)

    with pytest.raises(ModelGatewayError) as error:
        adapter.complete(request(), binding())

    assert "HTTP 401" in str(error.value)
    assert "test-secret" not in str(error.value)
    client.close()


def test_gateway_failure_is_recorded_on_run_and_agent_run():
    runtime = LocalPlatformRuntime()
    runtime.model_gateway = ModelGateway({})

    with pytest.raises(ModelGatewayError):
        runtime.run_agent("Summarize the handbook")

    run = runtime.runs.list()[0]
    agent_run = runtime.agent_runs.list()[0]
    assert run.status is RunStatus.FAILED
    assert agent_run.status is RunStatus.FAILED
    assert run.error is not None
    assert run.error.code == "model.adapter_missing"
    assert any(
        event.action == "agent.run.failed" and event.target_id == run.run_id
        for event in runtime.audit_events.list()
    )


def test_agent_output_schema_is_published_enforced_and_saved(monkeypatch):
    runtime = LocalPlatformRuntime()
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    agent = runtime.agent_service.create_agent(
        name="Structured agent",
        instructions="Return the requested answer.",
        output_schema=schema,
    )

    def complete(_request, selected_binding):
        return ModelResponse(
            provider_id=selected_binding.provider.id,
            model_id=selected_binding.profile.model,
            content='{"answer":"ready"}',
            usage=ModelUsage(input_tokens=8, output_tokens=4, total_tokens=12),
        )

    monkeypatch.setattr(runtime.model, "complete", complete)
    result = runtime.run_agent("Answer with ready", agent_id=agent.id)

    assert (
        runtime.agent_versions.get(agent.current_version_id).model_dump(mode="json")[
            "output_schema"
        ]
        == schema
    )
    assert result.agent_run.output_summary is not None
    assert result.agent_run.output_summary["structured_output"] == {"answer": "ready"}
    assert result.agent_run.output_summary["response"] == '{"answer":"ready"}'
    assert json.loads(result.artifact.summary) == {"answer": "ready"}


def test_invalid_structured_model_output_fails_run_and_retains_usage(monkeypatch):
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="Strict structured agent",
        instructions="Return a JSON answer.",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )

    def complete(_request, selected_binding):
        return ModelResponse(
            provider_id=selected_binding.provider.id,
            model_id=selected_binding.profile.model,
            content='{"answer":42}',
            usage=ModelUsage(input_tokens=9, output_tokens=3, total_tokens=12),
        )

    monkeypatch.setattr(runtime.model, "complete", complete)
    with pytest.raises(ModelGatewayError, match="configured output schema") as error:
        runtime.run_agent("Answer in the configured format", agent_id=agent.id)

    run = runtime.runs.list()[0]
    agent_run = runtime.agent_runs.list()[0]
    assert error.value.code == "agent.output_invalid"
    assert run.status is RunStatus.FAILED
    assert agent_run.status is RunStatus.FAILED
    assert agent_run.input_tokens == 9
    assert agent_run.output_tokens == 3
    assert agent_run.resolved_model_id == "weaves-deterministic-v1"
    failure_audit = next(
        event
        for event in runtime.audit_events.list()
        if event.action == "agent.run.failed" and event.target_id == run.run_id
    )
    assert failure_audit.metadata["output_schema_validation_failed"] is True


def test_model_provider_and_profile_writes_roll_back_with_failed_audit(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    original_audit_create = runtime.audit_events.create
    original_secret_put = runtime.secret_store.put
    secret_refs = []

    def tracked_secret_put(secret):
        secret_ref = original_secret_put(secret)
        secret_refs.append(secret_ref)
        return secret_ref

    def fail_audit_create(_record):
        raise RuntimeError("simulated audit store failure")

    before_provider_ids = {item.id for item in runtime.providers.list()}
    before_profile_ids = {item.id for item in runtime.model_profiles.list()}
    monkeypatch.setattr(runtime.secret_store, "put", tracked_secret_put)
    monkeypatch.setattr(runtime.audit_events, "create", fail_audit_create)

    with pytest.raises(RuntimeError, match="simulated audit store failure"):
        runtime.model_service.create_model_provider(
            provider_type=ModelProviderType.OPENAI,
            display_name="Uncommitted provider",
            api_key="provider-secret",
            initial_model="model-uncommitted",
        )

    assert {item.id for item in runtime.providers.list()} == before_provider_ids
    assert {item.id for item in runtime.model_profiles.list()} == before_profile_ids
    with pytest.raises(KeyError):
        runtime.secret_store.get(secret_refs[-1])

    monkeypatch.setattr(runtime.audit_events, "create", original_audit_create)
    provider = runtime.model_service.create_model_provider(
        provider_type=ModelProviderType.OPENAI,
        display_name="Committed provider",
        api_key="committed-secret",
        initial_model="model-committed",
    )
    profile = next(
        item
        for item in runtime.model_profiles.list()
        if item.provider_id == provider.id
    )
    original_profile = runtime.model_profiles.get(profile.id)

    monkeypatch.setattr(runtime.audit_events, "create", fail_audit_create)
    with pytest.raises(RuntimeError, match="simulated audit store failure"):
        runtime.model_service.update_model_profile(profile.id, default_temperature=0.9)
    assert runtime.model_profiles.get(profile.id) == original_profile

    original_secret_ref = provider.auth_ref
    original_secret = runtime.secret_store.get(original_secret_ref)
    with pytest.raises(RuntimeError, match="simulated audit store failure"):
        runtime.model_service.rotate_model_provider_credential(
            provider.id, "replacement-secret"
        )
    assert runtime.providers.get(provider.id).auth_ref == original_secret_ref
    assert runtime.secret_store.get(original_secret_ref) == original_secret
    with pytest.raises(KeyError):
        runtime.secret_store.get(secret_refs[-1])


def test_model_catalog_refresh_writes_profiles_and_audit_atomically(monkeypatch):
    runtime = LocalPlatformRuntime()
    provider = runtime.model_service.create_model_provider(
        provider_type=ModelProviderType.OPENAI,
        display_name="Catalog provider",
        api_key="provider-secret",
        initial_model="model-initial",
    )
    adapter = runtime.model_adapters[ModelProviderType.OPENAI]
    monkeypatch.setattr(
        adapter,
        "list_models",
        lambda _provider: [{"id": "model-initial"}, {"id": "model-discovered"}],
    )
    original_audit_create = runtime.audit_events.create

    def fail_audit_create(_record):
        raise RuntimeError("simulated audit store failure")

    monkeypatch.setattr(runtime.audit_events, "create", fail_audit_create)
    with pytest.raises(RuntimeError, match="simulated audit store failure"):
        runtime.model_service.refresh_provider_model_catalog(provider.id)

    assert runtime.providers.get(provider.id).allowed_models == ["model-initial"]
    discovered_profile_id = (
        "model-"
        + hashlib.sha256(
            (provider.id + "model-discovered").encode("utf-8")
        ).hexdigest()[:24]
    )
    with pytest.raises(KeyError):
        runtime.model_profiles.get(discovered_profile_id)

    monkeypatch.setattr(runtime.audit_events, "create", original_audit_create)
    profiles = runtime.model_service.refresh_provider_model_catalog(provider.id)
    assert {item.model for item in profiles} == {"model-initial", "model-discovered"}
    assert runtime.providers.get(provider.id).allowed_models == [
        "model-initial",
        "model-discovered",
    ]
    assert any(
        event.action == "model_provider.catalog_refreshed"
        and event.target_id == provider.id
        for event in runtime.audit_events.list()
    )


def test_disabled_provider_catalog_refresh_does_not_call_adapter(monkeypatch):
    runtime = LocalPlatformRuntime()
    provider = runtime.model_service.create_model_provider(
        provider_type=ModelProviderType.OPENAI,
        display_name="Disabled catalog provider",
        api_key="provider-secret",
        initial_model="model-initial",
    )
    runtime.providers.put(
        provider.model_copy(update={"status": ModelProviderStatus.DISABLED})
    )
    adapter = runtime.model_adapters[ModelProviderType.OPENAI]
    monkeypatch.setattr(
        adapter,
        "list_models",
        lambda _provider: pytest.fail("disabled providers must not be contacted"),
    )

    with pytest.raises(ModelGatewayError, match="Model provider is disabled"):
        runtime.model_service.refresh_provider_model_catalog(provider.id)


def test_environment_provider_bootstrap_supports_anthropic(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER_TYPE", "anthropic")
    monkeypatch.setenv("MODEL_API_KEY", "anthropic-environment-key")
    monkeypatch.setenv("MODEL_NAME", "claude-3-5-sonnet")
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)

    runtime = LocalPlatformRuntime()
    runtime.bootstrap()

    provider = runtime.providers.get("configured-model-provider")
    assert provider.provider_type is ModelProviderType.ANTHROPIC
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.allowed_models == ["claude-3-5-sonnet"]
    assert runtime.default_model_profile_id == "configured-model-profile"


def test_environment_provider_rejects_local_provider_type(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER_TYPE", "local")
    monkeypatch.setenv("MODEL_API_KEY", "environment-key")

    with pytest.raises(ValueError, match="configured external provider"):
        LocalPlatformRuntime()
