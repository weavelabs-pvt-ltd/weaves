"""Tenant-aware operations for public and organization-owned descriptors."""

from typing import Protocol, runtime_checkable

from contracts.v1 import PluginDescriptor


@runtime_checkable
class PluginCatalog(Protocol):
    """Keep catalog ownership separate from the descriptor's legacy wire format."""

    def create_owned_descriptor(
        self, descriptor: PluginDescriptor, org_id: str
    ) -> PluginDescriptor:
        """Create a custom descriptor with explicit, immutable ownership."""
        ...

    def visible_descriptors(self, org_id: str) -> tuple[PluginDescriptor, ...]:
        """Read public built-ins and this organization's custom definitions."""
        ...

    def visible_descriptor(self, plugin_id: str, org_id: str) -> PluginDescriptor:
        """Read one visible definition, rejecting quarantined or foreign IDs."""
        ...

    def ownership_state(self, plugin_id: str) -> str | None:
        """Return persisted ownership state, or None for an unmigrated record."""
        ...

    def has_installations(self, plugin_id: str) -> bool:
        """Check references without loading other tenants' connection payloads."""
        ...

    def delete_unreferenced_descriptor(self, plugin_id: str) -> bool:
        """Delete only an unreferenced custom definition, retaining quarantine."""
        ...
