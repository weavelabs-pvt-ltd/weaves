"""Shared visibility policy for legacy connector descriptor records."""

from application.ports.repository import Repository
from contracts.v1 import PluginDescriptor, PluginInstallation
from contracts.v1.plugin_visibility import (
    BUILTIN_PLUGIN_TYPES,
    descriptor_owners,
    descriptor_visible,
)


def visible_descriptors(
    descriptors: Repository[PluginDescriptor],
    installations: Repository[PluginInstallation],
    org_id: str,
) -> tuple[PluginDescriptor, ...]:
    """Filter catalogue metadata without changing legacy record payloads."""
    owners = descriptor_owners(installations.list())
    return tuple(
        item for item in descriptors.list() if descriptor_visible(item, org_id, owners)
    )


def scoped_descriptor(
    descriptors: Repository[PluginDescriptor],
    installations: Repository[PluginInstallation],
    plugin_id: str,
    org_id: str,
) -> PluginDescriptor:
    """Reject foreign, orphaned, or ambiguous definitions before dispatch."""
    descriptor = descriptors.get(plugin_id)
    if BUILTIN_PLUGIN_TYPES.get(descriptor.plugin_id) != descriptor.plugin_type:
        owners = descriptor_owners(installations.list())
        if not descriptor_visible(descriptor, org_id, owners):
            raise KeyError("Plugin descriptor not found in organization scope")
    return descriptor
