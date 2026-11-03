from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from contracts.v1 import (
    AgentBudget,
    AgentBundle,
    AgentDefinition,
    AgentSkillSnapshot,
    AgentStatus,
    AgentVersion,
    ModelProfile,
    ModelProfileStatus,
    ModelProvider,
    ModelProviderStatus,
    ModelProviderType,
)

NOW = datetime.now(UTC)


def provider(**overrides):
    values = {
        "id": "provider-a",
        "org_id": "org-a",
        "provider_type": ModelProviderType.OPENAI,
        "display_name": "Example model provider",
        "allowed_models": ["model-a"],
        "auth_ref": "secret-ref-a",
        "base_url": "https://models.example.com/v1",
        "status": ModelProviderStatus.ACTIVE,
    }
    return ModelProvider(**(values | overrides))


def profile(**overrides):
    values = {
        "id": "profile-a",
        "org_id": "org-a",
        "provider_id": "provider-a",
        "model": "model-a",
        "display_name": "Default model",
        "default_temperature": 0.2,
        "max_output_tokens": 2048,
        "cost_budget_usd": Decimal("5.00"),
        "status": ModelProfileStatus.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ModelProfile(**(values | overrides))


def definition(**overrides):
    values = {
        "id": "agent-a",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "name": "Support helper",
        "description": "Answers approved support questions.",
        "current_version_id": "agent-version-a",
        "status": AgentStatus.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return AgentDefinition(**(values | overrides))


def agent_version(**overrides):
    values = {
        "agent_version_id": "agent-version-a",
        "agent_id": "agent-a",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "version": 1,
        "persona": "A concise support assistant",
        "instructions": "Answer using approved company sources and cite them.",
        "model_profile_id": "profile-a",
        "allowed_capability_ids": ("knowledge.search",),
        "allowed_workflow_ids": ("support.triage",),
        "budget": AgentBudget(),
        "output_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
        },
        "created_at": NOW,
    }
    return AgentVersion(**(values | overrides))


def test_provider_rejects_raw_credentials():
    with pytest.raises(ValidationError):
        provider(api_key="raw-secret")
    with pytest.raises(ValidationError):
        provider(base_url="https://user:password@models.example.com/v1")
    with pytest.raises(ValidationError):
        provider(base_url="https://models.example.com/v1?api_key=raw-secret")


@pytest.mark.parametrize(
    "field,value",
    [
        ("default_temperature", 2.1),
        ("default_temperature", -0.1),
        ("max_output_tokens", 0),
        ("max_output_tokens", 32769),
        ("cost_budget_usd", Decimal("0")),
        ("cost_budget_usd", Decimal("100001")),
    ],
)
def test_model_profile_inference_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        profile(**{field: value})


def test_model_profile_fallback_cannot_reference_itself():
    with pytest.raises(ValidationError):
        profile(fallback_model_profile_id="profile-a")


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_runtime_seconds", 0),
        ("max_runtime_seconds", 3601),
        ("max_tool_calls", 101),
        ("max_output_tokens", 0),
        ("max_run_cost_usd", Decimal("0")),
    ],
)
def test_agent_budget_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        AgentBudget(**{field: value})


def test_profile_and_provider_composition_checks_scope_and_allowlist():
    from contracts.v1 import ModelProfileBinding

    ModelProfileBinding(provider=provider(), profile=profile())
    with pytest.raises(ValidationError):
        ModelProfileBinding(provider=provider(), profile=profile(org_id="org-b"))
    with pytest.raises(ValidationError):
        ModelProfileBinding(provider=provider(), profile=profile(model="model-b"))


def test_agent_version_is_immutable():
    version = agent_version()
    with pytest.raises(ValidationError):
        version.instructions = "Changed after publish"
    with pytest.raises(TypeError):
        version.output_schema["type"] = "string"
    with pytest.raises(TypeError):
        version.output_schema["properties"]["answer"]["type"] = "integer"


def test_agent_version_rejects_duplicate_pinned_skills():
    skill = AgentSkillSnapshot(
        skill_id="evidence-based-research",
        version=1,
        name="Evidence-based research",
        instructions="Use approved sources and cite evidence.",
    )
    with pytest.raises(ValidationError, match="agent skills must be unique"):
        agent_version(skills=(skill, skill))


def test_agent_version_scope_matches_its_definition():
    AgentBundle(definition=definition(), version=agent_version())
    with pytest.raises(ValidationError):
        AgentBundle(
            definition=definition(workspace_id="workspace-a"),
            version=agent_version(workspace_id="workspace-b"),
        )
    with pytest.raises(ValidationError):
        AgentBundle(
            definition=definition(current_version_id="old-version"),
            version=agent_version(),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_capability_ids", ("knowledge.search", "knowledge.search")),
        ("allowed_workflow_ids", ("support.triage", "support.triage")),
        ("persona", "  "),
        ("instructions", "  "),
    ],
)
def test_agent_version_rejects_duplicates_and_blank_text(field, value):
    with pytest.raises(ValidationError):
        agent_version(**{field: value})


def test_output_schema_accepts_only_json_compatible_values():
    version = agent_version()
    assert version.model_dump(mode="json")["output_schema"]["type"] == "object"
    with pytest.raises(ValidationError):
        agent_version(output_schema={"callable": object()})
    with pytest.raises(ValidationError):
        agent_version(output_schema={"not-finite": float("nan")})
    with pytest.raises(ValidationError, match="valid JSON Schema"):
        agent_version(output_schema={"type": "not-a-json-schema-type"})


def test_active_definition_requires_current_version():
    with pytest.raises(ValidationError):
        definition(current_version_id=None)
