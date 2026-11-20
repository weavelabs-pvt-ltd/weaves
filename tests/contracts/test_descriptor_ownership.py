"""Ambiguous or orphaned legacy descriptors must fail closed."""

import pytest

from application.descriptor_catalog import scoped_descriptor, visible_descriptors
from contracts.v1 import PluginInstallationStatus
from contracts.v1.plugin_visibility import descriptor_owners
from infrastructure.integrations.plugin_gateway import PluginGateway
from infrastructure.persistence.memory_repository import InMemoryRepository
from shared.errors import PluginGatewayError
from tests.contracts.test_plugin_workflow_contracts import descriptor, installation
from tests.support.runtime import LocalPlatformRuntime


def _catalog(records):
    plugins = InMemoryRepository("plugin_id")
    installations = InMemoryRepository("plugin_installation_id")
    plugins.create(descriptor())
    for record in records:
        installations.create(record)
    return plugins, installations


def test_same_org_workspaces_and_disabled_installations_keep_ownership():
    records = (
        installation(),
        installation(
            plugin_installation_id="second-installation",
            workspace_id="workspace-a2",
            status=PluginInstallationStatus.DISABLED,
        ),
    )
    plugins, installations = _catalog(records)
    assert descriptor_owners(installations.list()) == {"custom-mcp": {"org-a"}}
    assert len(visible_descriptors(plugins, installations, "org-a")) == 1
    assert visible_descriptors(plugins, installations, "org-b") == ()
    installations.delete("installation-a")
    assert len(visible_descriptors(plugins, installations, "org-a")) == 1
    installations.delete("second-installation")
    assert visible_descriptors(plugins, installations, "org-a") == ()
    assert plugins.get("custom-mcp").display_name == "Custom MCP"


@pytest.mark.parametrize("organization", ["org-a", "org-b"])
def test_conflicting_org_references_are_quarantined_for_everyone(organization):
    plugins, installations = _catalog(
        (
            installation(),
            installation(
                plugin_installation_id="foreign-installation",
                org_id="org-b",
                workspace_id="workspace-b",
            ),
        )
    )
    assert visible_descriptors(plugins, installations, organization) == ()
    with pytest.raises(KeyError, match="organization scope"):
        scoped_descriptor(plugins, installations, "custom-mcp", organization)
    assert len(plugins.list()) == 1
    assert len(installations.list()) == 2


@pytest.mark.parametrize("plugin_id", ["github", "slack", "jira", "mcp"])
def test_imported_descriptor_cannot_become_public_by_claiming_a_builtin_id(plugin_id):
    plugins, installations = _catalog(())
    plugins.create(descriptor(plugin_id=plugin_id, plugin_type="mcp"))
    assert visible_descriptors(plugins, installations, "org-b") == ()


class _Provider:
    def __init__(self):
        self.calls = []

    def invoke(self, capability_id, payload):
        self.calls.append((capability_id, payload))
        return {}


def test_gateway_rechecks_descriptor_ownership_before_any_tool_dispatch():
    plugins, installations = _catalog((installation(),))
    provider = _Provider()
    gateway = PluginGateway(plugins, installations, {"mcp": provider})
    runtime = LocalPlatformRuntime(postgres_store=None)
    try:
        runtime.bootstrap()
        version = runtime.agent_versions.get("local-assistant-v1").model_copy(
            update={
                "org_id": "org-a",
                "workspace_id": "workspace-a",
                "allowed_capability_ids": ("custom.search",),
            }
        )
    finally:
        runtime.close()
    arguments = {
        "org_id": "org-a",
        "workspace_id": "workspace-a",
        "installation_id": "installation-a",
        "agent_version": version,
        "capability_id": "custom.search",
        "payload": {"query": "fixture"},
    }
    gateway.invoke(**arguments)
    assert len(provider.calls) == 1
    installations.create(
        installation(
            plugin_installation_id="foreign-installation",
            org_id="org-b",
            workspace_id="workspace-b",
        )
    )
    with pytest.raises(PluginGatewayError, match="descriptor was not found"):
        gateway.invoke(**arguments)
    assert len(provider.calls) == 1
