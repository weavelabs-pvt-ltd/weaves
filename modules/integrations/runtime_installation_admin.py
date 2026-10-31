"""Manage installed organization integrations and credentials."""

from __future__ import annotations

from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from clients.integrations.mcp import is_query_tool
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
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


class IntegrationInstallationAdminMixin(RuntimeCapabilityHost):
    """Own integration installationadmin operations."""

    def update_plugin_installation(
        self,
        installation_id: str,
        *,
        enabled_capability_ids: tuple[str, ...] | None = None,
        status: PluginInstallationStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Enable or disable registered capabilities for the workspace plugin."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        current = self.installations.get_scoped(
            installation_id, effective_org_id(), effective_workspace_id()
        )
        capabilities = (
            enabled_capability_ids
            if enabled_capability_ids is not None
            else current.enabled_capability_ids
        )
        descriptor = self.plugins.get(current.plugin_id)
        declared = {item.capability_id for item in descriptor.capability_manifest}
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("enabled capabilities must be unique")
        if not set(capabilities).issubset(declared):
            raise ValueError("only declared plugin capabilities can be enabled")
        updated = current.model_copy(
            update={
                "enabled_capability_ids": capabilities,
                "status": status or current.status,
                "updated_at": _now(),
            }
        )
        updated = PluginInstallation.model_validate(updated.model_dump())
        self.installations.put(updated)
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=current.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=current.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="plugin_installation.updated",
                target_type="plugin_installation",
                target_id=installation_id,
                summary="Plugin installation updated",
                request_id=installation_id,
                metadata={"enabled_capability_ids": list(capabilities)},
                created_at=_now(),
            )
        )
        return updated

    def discover_mcp_tools(
        self,
        *,
        endpoint_url: str,
        bearer_token: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[dict[str, Any], ...]:
        """Return a bounded MCP tool catalog without storing its credential."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        try:
            tools = self.mcp_plugin.discover_tools(endpoint_url, bearer_token)
        except PluginGatewayError as exc:
            raise ValueError(exc.message) from exc
        return tuple(
            {
                "name": tool.get("name"),
                "title": str(tool.get("title") or tool.get("name") or "")[:160],
                "description": str(tool.get("description") or "")[:1000],
                "input_schema": tool.get("inputSchema"),
                "read_tool_supported": is_query_tool(tool),
            }
            for tool in tools
        )

    def rotate_plugin_credential(
        self,
        installation_id: str,
        api_token: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Replace an installed connector token without exposing the secret."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        if not api_token.strip():
            raise ValueError("connector API token is required")
        current = self.installations.get_scoped(
            installation_id, effective_org_id(), effective_workspace_id()
        )
        next_secret_ref = self.secret_store.put(api_token.strip())
        updated = current.model_copy(
            update={"auth_ref": next_secret_ref, "updated_at": _now()}
        )
        updated = PluginInstallation.model_validate(updated.model_dump())
        try:
            self.installations.put(updated)
        except Exception:
            self.secret_store.delete(next_secret_ref)
            raise
        if current.auth_ref:
            self.secret_store.delete(current.auth_ref)
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=current.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=current.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="plugin_installation.credential_rotated",
                target_type="plugin_installation",
                target_id=installation_id,
                summary="Connector credential rotated",
                request_id=installation_id,
                metadata={"plugin_id": current.plugin_id},
                created_at=_now(),
            )
        )
        return updated
