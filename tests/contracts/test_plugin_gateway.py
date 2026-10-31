import pytest

from infrastructure.integrations.plugin_gateway import PluginGatewayError
from tests.support.runtime import LocalPlatformRuntime


def test_plugin_gateway_runs_enabled_schema_validated_context_read():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    version = runtime.agent_versions.get("local-assistant-v1")

    result = runtime.plugin_gateway.invoke(
        org_id="local-org",
        workspace_id="local-workspace",
        installation_id="local-knowledge-installation",
        agent_version=version,
        capability_id="knowledge.search",
        payload={"query": "security basics"},
    )

    assert result.output["documents"]
    assert result.duration_ms >= 0


def test_plugin_gateway_enforces_agent_allowlist_and_input_schema():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    version = runtime.agent_versions.get("local-assistant-v1")
    restricted_version = version.model_copy(update={"allowed_capability_ids": ()})

    with pytest.raises(PluginGatewayError, match="not allowed"):
        runtime.plugin_gateway.invoke(
            org_id="local-org",
            workspace_id="local-workspace",
            installation_id="local-knowledge-installation",
            agent_version=restricted_version,
            capability_id="knowledge.search",
            payload={"query": "security"},
        )
    with pytest.raises(PluginGatewayError, match="input is invalid"):
        runtime.plugin_gateway.invoke(
            org_id="local-org",
            workspace_id="local-workspace",
            installation_id="local-knowledge-installation",
            agent_version=version,
            capability_id="knowledge.search",
            payload={"secret": "not-allowed"},
        )


def test_plugin_gateway_rejects_workspace_mismatch():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    version = runtime.agent_versions.get("local-assistant-v1")

    with pytest.raises(PluginGatewayError, match="outside the requested scope"):
        runtime.plugin_gateway.invoke(
            org_id="local-org",
            workspace_id="another-workspace",
            installation_id="local-knowledge-installation",
            agent_version=version,
            capability_id="knowledge.search",
            payload={"query": "security"},
        )
