"""Manage installed organization integrations and credentials."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
    PluginDescriptor,
    PluginInstallation,
    PluginInstallationStatus,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import PluginGatewayError
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class _HealthcheckPlugin(Protocol):
    """Connector protocol required by installation health checks."""

    def healthcheck(self, installation: PluginInstallation) -> dict[str, str]:
        """Validate a stored connection without exposing its credential."""
        ...


class IntegrationInstallationHealthMixin(RuntimeCapabilityHost):
    """Own integration installationhealth operations."""

    def healthcheck_plugin_installation(
        self,
        installation_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> dict[str, str]:
        """Validate a connector credential and update its health timestamp."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        installation = self.installations.get_scoped(
            installation_id, effective_org_id(), effective_workspace_id()
        )
        plugins: dict[str, _HealthcheckPlugin] = {
            "github": self.github_plugin,
            "slack": self.slack_plugin,
            "jira_cloud": self.jira_plugin,
            "jira": self.jira_plugin,
            "mcp": self.mcp_plugin,
        }
        descriptor = self.plugins.get(installation.plugin_id)
        details = self._record_plugin_healthcheck(
            descriptor,
            installation,
            plugins,
            requested_by_principal_id,
            installation_id,
        )
        now = _now()
        checked = installation.model_copy(
            update={
                "status": (
                    PluginInstallationStatus.DISABLED
                    if installation.status is PluginInstallationStatus.DISABLED
                    else PluginInstallationStatus.ACTIVE
                ),
                "last_healthcheck_at": now,
                "updated_at": now,
            }
        )
        self.installations.put(PluginInstallation.model_validate(checked.model_dump()))
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=installation.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=installation.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="plugin_installation.healthchecked",
                target_type="plugin_installation",
                target_id=installation_id,
                summary=f"{installation.plugin_id} installation health check succeeded",
                request_id=installation_id,
                metadata=details,
                created_at=now,
            )
        )
        return details

    def _record_plugin_healthcheck(
        self,
        descriptor: PluginDescriptor,
        installation: PluginInstallation,
        plugins: Mapping[str, _HealthcheckPlugin],
        requested_by_principal_id: str,
        installation_id: str,
    ) -> dict[str, str]:
        """Record the result of a plugin health check."""
        try:
            if descriptor.plugin_type == "knowledge_base":
                documents = self.knowledge_documents.list_scoped(
                    installation.org_id, installation.workspace_id
                )
                details = {
                    "documents": str(len(documents)),
                    "enabled_capabilities": str(
                        len(installation.enabled_capability_ids)
                    ),
                }
            else:
                plugin = plugins.get(descriptor.plugin_type)
                if plugin is None:
                    raise ValueError("health checks are not supported for this plugin")
                details = plugin.healthcheck(installation)
        except PluginGatewayError:
            now = _now()
            failed = installation.model_copy(
                update={
                    "status": PluginInstallationStatus.ERROR,
                    "last_healthcheck_at": now,
                    "updated_at": now,
                }
            )
            self.installations.put(
                PluginInstallation.model_validate(failed.model_dump())
            )
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=installation.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=installation.workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="plugin_installation.healthcheck_failed",
                    target_type="plugin_installation",
                    target_id=installation_id,
                    summary=(
                        f"{installation.plugin_id} installation health check failed"
                    ),
                    request_id=installation_id,
                    metadata={},
                    created_at=now,
                )
            )
            raise
        return details
