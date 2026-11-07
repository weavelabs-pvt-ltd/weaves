from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.v1 import (
    CapabilityKind,
    CapabilitySpec,
    PluginDescriptor,
    PluginDescriptorStatus,
    PluginInstallation,
    PluginInstallationBinding,
    PluginInstallationStatus,
    RiskLevel,
    WorkflowBundle,
    WorkflowConditionOperator,
    WorkflowDefinition,
    WorkflowJoinPolicy,
    WorkflowParallelGroup,
    WorkflowStatus,
    WorkflowStepCondition,
    WorkflowStepDependency,
    WorkflowVersion,
)

NOW = datetime.now(UTC)
OBJECT_SCHEMA = {"type": "object", "properties": {}}


def capability(**overrides):
    values = {
        "capability_id": "custom.search",
        "name": "Search a connected service",
        "description": "Find relevant content from a connected service.",
        "kind": CapabilityKind.CONTEXT_READ,
        "risk_level": RiskLevel.LOW,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "output_schema": OBJECT_SCHEMA,
        "timeout_seconds": 30,
        "approval_supported": False,
    }
    return CapabilitySpec(**(values | overrides))


def descriptor(**overrides):
    values = {
        "plugin_id": "custom-mcp",
        "plugin_type": "mcp",
        "display_name": "Custom MCP",
        "description": "Approved company policy and process documents.",
        "capability_manifest": (capability(),),
        "auth_schema": {
            "type": "object",
            "properties": {"api_key": {"type": "string"}},
        },
        "configuration_schema": OBJECT_SCHEMA,
        "status": PluginDescriptorStatus.ACTIVE,
    }
    return PluginDescriptor(**(values | overrides))


def installation(**overrides):
    values = {
        "plugin_installation_id": "installation-a",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "plugin_id": "custom-mcp",
        "display_name": "Company MCP",
        "auth_ref": "vault-ref-a",
        "configuration": {"endpoint_url": "https://mcp.example.test"},
        "enabled_capability_ids": ("custom.search",),
        "status": PluginInstallationStatus.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
        "last_healthcheck_at": NOW,
    }
    return PluginInstallation(**(values | overrides))


def workflow_definition(**overrides):
    values = {
        "workflow_id": "support-triage",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "name": "Support triage",
        "description": "Classify and summarize a support request.",
        "current_version_id": "support-triage-v1",
        "status": WorkflowStatus.ACTIVE,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return WorkflowDefinition(**(values | overrides))


def workflow_version(**overrides):
    values = {
        "workflow_version_id": "support-triage-v1",
        "workflow_id": "support-triage",
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "version": 1,
        "handler_key": "support.triage",
        "agent_version_ids": ("support-agent-v3",),
        "required_capability_ids": ("custom.search",),
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        },
        "configuration": {"locale": "en"},
        "created_at": NOW,
    }
    return WorkflowVersion(**(values | overrides))


def test_plugin_installation_requires_organization_and_workspace_scope():
    with pytest.raises(ValidationError):
        PluginInstallation(
            plugin_installation_id="installation-a",
            plugin_id="custom-mcp",
            display_name="Handbook",
            status=PluginInstallationStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )


@pytest.mark.parametrize(
    "configuration",
    [
        {"api_key": "raw-secret"},
        {"clientSecret": "raw-secret"},
        {"options": {"refresh_token": "raw-secret"}},
        {"headers": [{"Authorization": "Bearer secret"}]},
    ],
)
def test_plugin_installation_rejects_raw_credentials(configuration):
    with pytest.raises(ValidationError):
        installation(configuration=configuration)


def test_plugin_installation_rejects_credential_fields_outside_config():
    with pytest.raises(ValidationError):
        installation(api_key="raw-secret")


@pytest.mark.parametrize("field", ["input_schema", "output_schema"])
def test_capability_schemas_must_be_valid_json_schema(field):
    with pytest.raises(ValidationError):
        capability(**{field: {"type": "not-a-json-schema-type"}})


@pytest.mark.parametrize(
    "kind", [CapabilityKind.ACTION_WRITE, CapabilityKind.DELIVERY_SEND]
)
def test_write_and_delivery_capabilities_require_risk(kind):
    with pytest.raises(ValidationError):
        capability(kind=kind, risk_level=None)


def test_capability_manifest_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        descriptor(capability_manifest=(capability(), capability()))


def test_plugin_installation_only_enables_declared_capabilities():
    PluginInstallationBinding(descriptor=descriptor(), installation=installation())
    with pytest.raises(ValidationError):
        PluginInstallationBinding(
            descriptor=descriptor(),
            installation=installation(enabled_capability_ids=("jira.search",)),
        )


def test_workflow_version_is_immutable_including_nested_configuration():
    version = workflow_version()
    with pytest.raises(ValidationError):
        version.handler_key = "changed.handler"
    with pytest.raises(TypeError):
        version.configuration["locale"] = "fr"


@pytest.mark.parametrize("handler_key", ["Support.triage", "not a handler", "x"])
def test_workflow_version_requires_registered_looking_handler_key(handler_key):
    with pytest.raises(ValidationError):
        workflow_version(handler_key=handler_key)


def test_workflow_agent_version_references_must_be_unique():
    with pytest.raises(ValidationError):
        workflow_version(agent_version_ids=("agent-v1", "agent-v1"))


def test_workflow_parallel_groups_are_unique_and_cannot_overlap():
    group = WorkflowParallelGroup(agent_ids=("reviewer", "risk-checker"))
    assert group.agent_ids == ("reviewer", "risk-checker")
    with pytest.raises(ValidationError, match="unique"):
        WorkflowParallelGroup(agent_ids=("reviewer", "reviewer"))
    with pytest.raises(ValidationError, match="share workflow steps"):
        workflow_version(
            parallel_groups=(
                group,
                WorkflowParallelGroup(agent_ids=("risk-checker", "summary")),
            )
        )


def test_workflow_step_dependencies_validate_join_policy_and_references():
    dependency = WorkflowStepDependency(
        target_agent_id="summary",
        depends_on_agent_ids=("reviewer", "risk-checker"),
        join_policy=WorkflowJoinPolicy.ANY,
    )
    assert dependency.join_policy is WorkflowJoinPolicy.ANY
    with pytest.raises(ValidationError, match="unique"):
        WorkflowStepDependency(
            target_agent_id="summary",
            depends_on_agent_ids=("reviewer", "reviewer"),
        )
    with pytest.raises(ValidationError, match="itself"):
        WorkflowStepDependency(
            target_agent_id="summary", depends_on_agent_ids=("summary",)
        )
    with pytest.raises(ValidationError, match="dependency policy"):
        workflow_version(
            step_dependencies=(
                dependency,
                dependency.model_copy(update={"join_policy": WorkflowJoinPolicy.ALL}),
            )
        )


def test_workflow_step_condition_requires_a_valid_operator_and_json_pointer():
    condition = WorkflowStepCondition(
        source_agent_id="router",
        target_agent_id="reviewer",
        json_pointer="/decision/route",
        operator=WorkflowConditionOperator.EQUALS,
        expected_value="review",
    )
    assert condition.expected_value == "review"
    with pytest.raises(ValidationError, match="expected_value"):
        WorkflowStepCondition(
            source_agent_id="router",
            target_agent_id="reviewer",
            json_pointer="/decision/route",
            operator=WorkflowConditionOperator.EQUALS,
        )
    with pytest.raises(ValidationError, match="invalid escape"):
        WorkflowStepCondition(
            source_agent_id="router",
            target_agent_id="reviewer",
            json_pointer="/decision/~2route",
            operator=WorkflowConditionOperator.EXISTS,
            expected_value=None,
        )
    with pytest.raises(ValidationError, match="credential-shaped fields"):
        WorkflowStepCondition(
            source_agent_id="router",
            target_agent_id="reviewer",
            json_pointer="/decision",
            operator=WorkflowConditionOperator.EQUALS,
            expected_value={"access_token": "never-store"},
        )


def test_workflow_inputs_and_outputs_must_be_valid_json_schema():
    with pytest.raises(ValidationError):
        workflow_version(output_schema={"type": "not-a-json-schema-type"})


def test_workflow_configuration_cannot_embed_credentials():
    with pytest.raises(ValidationError):
        workflow_version(configuration={"connection": {"access_token": "raw-secret"}})


def test_workflow_bundle_checks_definition_scope_and_version_pointer():
    WorkflowBundle(definition=workflow_definition(), version=workflow_version())
    with pytest.raises(ValidationError):
        WorkflowBundle(
            definition=workflow_definition(workspace_id="workspace-other"),
            version=workflow_version(),
        )
    with pytest.raises(ValidationError):
        WorkflowBundle(
            definition=workflow_definition(current_version_id="old-version"),
            version=workflow_version(),
        )
