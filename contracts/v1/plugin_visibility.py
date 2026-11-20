"""Visibility rules for built-in and legacy imported connector definitions."""

from collections.abc import Iterable, Mapping, Set

from contracts.v1.plugins import PluginDescriptor, PluginInstallation

BUILTIN_PLUGIN_TYPES = {"github": "github", "slack": "slack", "jira": "jira_cloud"}


def descriptor_owners(
    installations: Iterable[PluginInstallation],
) -> dict[str, set[str]]:
    """Infer ownership from every installation, including disabled ones.

    Multiple workspaces in one organization are valid; references from two
    organizations are ambiguous and must never make a definition public.
    """
    owners: dict[str, set[str]] = {}
    for installation in installations:
        owners.setdefault(installation.plugin_id, set()).add(installation.org_id)
    return owners


def descriptor_visible(
    descriptor: PluginDescriptor,
    org_id: str,
    owners: Mapping[str, Set[str]],
) -> bool:
    """Expose known built-ins or custom definitions with one proven tenant."""
    if BUILTIN_PLUGIN_TYPES.get(descriptor.plugin_id) == descriptor.plugin_type:
        return True
    return owners.get(descriptor.plugin_id) == {org_id}
