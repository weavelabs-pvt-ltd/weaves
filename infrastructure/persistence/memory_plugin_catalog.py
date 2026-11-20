"""Explicit catalog ownership for local runtimes, with legacy reader support."""

from typing import cast

from contracts.v1 import PluginDescriptor, PluginInstallation
from contracts.v1.plugin_visibility import (
    BUILTIN_PLUGIN_TYPES,
    descriptor_owners,
    descriptor_visible,
)
from infrastructure.persistence.memory_repository import InMemoryRepository


class InMemoryPluginCatalog(InMemoryRepository[PluginDescriptor]):
    """Preserve an owner's identity even when its last installation is removed."""

    def __init__(self, installations: InMemoryRepository[PluginInstallation]) -> None:
        """Bind installation references for legacy descriptors."""
        super().__init__("plugin_id")
        self._installations = installations
        self._owner_ids: dict[str, str] = {}

    def create_owned_descriptor(
        self, descriptor: PluginDescriptor, org_id: str
    ) -> PluginDescriptor:
        """Attach ownership atomically without modifying serialized descriptors."""
        if not org_id or descriptor.plugin_id in BUILTIN_PLUGIN_TYPES:
            raise ValueError("Custom descriptor requires an organization and custom ID")
        with self._lock:
            result = super().create(descriptor)
            self._owner_ids[descriptor.plugin_id] = org_id
            return result

    def visible_descriptors(self, org_id: str) -> tuple[PluginDescriptor, ...]:
        """Filter explicit owners and safely infer unmigrated legacy owners."""
        owners = descriptor_owners(self._installations.list())
        with self._lock:
            return tuple(
                self._clone(item)
                for item in self._records.values()
                if self._visible(item, org_id, owners)
            )

    def visible_descriptor(self, plugin_id: str, org_id: str) -> PluginDescriptor:
        """Resolve one definition without granting access through a foreign ref."""
        owners = descriptor_owners(self._installations.list())
        with self._lock:
            descriptor = self.get(plugin_id)
            if not self._visible(descriptor, org_id, owners):
                raise KeyError("Plugin descriptor not found in organization scope")
            return descriptor

    def _visible(
        self, item: PluginDescriptor, org_id: str, owners: dict[str, set[str]]
    ) -> bool:
        owner = self._owner_ids.get(item.plugin_id)
        if owner is not None:
            return owner == org_id and owners.get(item.plugin_id, {owner}) == {owner}
        return descriptor_visible(item, org_id, owners)

    def ownership_state(self, plugin_id: str) -> str | None:
        """Distinguish explicit custom ownership from legacy inferred records."""
        with self._lock:
            return "owned" if plugin_id in self._owner_ids else None

    def has_installations(self, plugin_id: str) -> bool:
        """Retain referenced definitions during connection cleanup."""
        return any(item.plugin_id == plugin_id for item in self._installations.list())

    def delete_unreferenced_descriptor(self, plugin_id: str) -> bool:
        """Keep local cleanup atomic with installation reference changes."""
        from infrastructure.persistence.unit_of_work import InMemoryUnitOfWork

        with InMemoryUnitOfWork((self, self._installations)):
            if plugin_id in BUILTIN_PLUGIN_TYPES or self.has_installations(plugin_id):
                return False
            if plugin_id not in self._records:
                return False
            self.delete(plugin_id)
            return True

    def delete(self, record_id: str) -> None:
        """Remove a definition and its metadata together."""
        with self._lock:
            super().delete(record_id)
            self._owner_ids.pop(record_id, None)

    def snapshot_state(self) -> object:
        """Include ownership in the local unit-of-work rollback snapshot."""
        return super().snapshot_state(), dict(self._owner_ids)

    def restore_state(self, snapshot: object) -> None:
        """Restore descriptor and owner data atomically after a failed operation."""
        records, owners = cast(tuple[object, dict[str, str]], snapshot)
        super().restore_state(records)
        self._owner_ids = owners
