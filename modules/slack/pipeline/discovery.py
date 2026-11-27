"""DISCOVER: which read tools the organization has and whether each is usable.

Pure reads of the organization's own records: the enabled read capabilities of
its connector installations and the ``slack_connector_health`` snapshots. No
connector is called here. A tool whose snapshot is missing, older than fifteen
minutes or errored has ``health=None``: the pipeline probes it live (3 s) only if
the plan turns out to need it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime

from application.descriptor_catalog import scoped_descriptor
from application.ports.repository import Repository
from contracts.v1 import (
    CapabilityKind,
    CapabilitySpec,
    PluginDescriptor,
    PluginInstallation,
    PluginInstallationStatus,
)
from modules.slack.access.models import ToolHealth
from modules.slack.pipeline.health import (
    LIVE_PROBE_SECONDS,
    SNAPSHOT_TTL,
    HealthSnapshots,
)
from modules.slack.pipeline.models import DiscoveredTool

_FINAL = {
    "token_expired": ToolHealth.TOKEN_EXPIRED,
    "rate_limited": ToolHealth.UNAVAILABLE,
    "disabled": ToolHealth.DISABLED,
}


class ToolDiscovery:
    """List the organization's read tools and settle a needed tool's health."""

    def __init__(
        self,
        *,
        installations: Repository[PluginInstallation],
        plugins: Repository[PluginDescriptor],
        snapshots: HealthSnapshots,
    ) -> None:
        """Bind the connector records and the health snapshot cache."""
        self._installations = installations
        self._plugins = plugins
        self._snapshots = snapshots

    def discover(
        self, org_id: str, workspace_id: str, now: datetime
    ) -> tuple[DiscoveredTool, ...]:
        """Every enabled read capability, each with its status as of ``now``."""
        found: list[DiscoveredTool] = []
        for installation in self._installations.list_scoped(org_id, workspace_id):
            try:
                descriptor = scoped_descriptor(
                    self._plugins,
                    self._installations,
                    installation.plugin_id,
                    installation.org_id,
                )
            except KeyError:
                continue
            health, was_error = self._status(installation, now)
            enabled = set(installation.enabled_capability_ids)
            for capability in descriptor.capability_manifest:
                if (
                    capability.kind is not CapabilityKind.CONTEXT_READ
                    or capability.capability_id not in enabled
                ):
                    continue
                found.append(
                    _tool(installation, descriptor, capability, health, was_error)
                )
        return _without_ambiguous(found)

    def _status(
        self, installation: PluginInstallation, now: datetime
    ) -> tuple[ToolHealth | None, bool]:
        if installation.status is PluginInstallationStatus.DISABLED:
            return ToolHealth.DISABLED, False
        snapshot = self._snapshots.read(
            installation.org_id, installation.plugin_installation_id
        )
        if snapshot is None or now - snapshot.checked_at > SNAPSHOT_TTL:
            return None, False
        if snapshot.status == "healthy":
            return ToolHealth.HEALTHY, False
        if snapshot.status == "error":
            return None, True
        return _FINAL.get(snapshot.status, ToolHealth.UNAVAILABLE), False

    def settle(self, tool: DiscoveredTool, probed: dict[str, ToolHealth]) -> ToolHealth:
        """The tool's health, probing live (3 s) once per installation if unknown."""
        if tool.health is not None:
            return tool.health
        known = probed.get(tool.installation_id)
        if known is not None:
            return known
        status, _ = self._snapshots.probe_and_store(
            tool.org_id, tool.installation_id, timeout=LIVE_PROBE_SECONDS
        )
        if status == "healthy":
            health = ToolHealth.HEALTHY
        elif status == "token_expired":
            health = ToolHealth.TOKEN_EXPIRED
        else:
            health = ToolHealth.ERROR if tool.was_error else ToolHealth.STALE
        probed[tool.installation_id] = health
        return health


def _tool(
    installation: PluginInstallation,
    descriptor: PluginDescriptor,
    capability: CapabilitySpec,
    health: ToolHealth | None,
    was_error: bool,
) -> DiscoveredTool:
    config = installation.configuration
    return DiscoveredTool(
        tool_id=capability.capability_id,
        installation_id=installation.plugin_installation_id,
        org_id=installation.org_id,
        label=installation.display_name,
        name=capability.name,
        description=capability.description,
        plugin_type=descriptor.plugin_type,
        health=health,
        was_error=was_error,
        projects=_strings(config.get("project_keys")),
        repositories=frozenset(r.lower() for r in _strings(config.get("repositories"))),
    )


def _strings(value: object) -> frozenset[str]:
    """The string items of a configured list (anything else is ignored)."""
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))


def _without_ambiguous(tools: list[DiscoveredTool]) -> tuple[DiscoveredTool, ...]:
    """A capability on several installations cannot run (the runtime refuses it)."""
    counts = Counter(tool.tool_id for tool in tools)
    return tuple(tool if counts[tool.tool_id] == 1 else _red(tool) for tool in tools)


def _red(tool: DiscoveredTool) -> DiscoveredTool:
    return replace(tool, health=ToolHealth.UNAVAILABLE, was_error=False)
