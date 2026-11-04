"""Create dynamic connector installations from trusted MCP tool catalogs."""

from __future__ import annotations

import json
from typing import Any

from pydantic import JsonValue, ValidationError

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from application.runtime_host import RuntimeCapabilityHost
from clients.integrations.mcp import (
    capability_id_for_tool,
    imported_capability,
    query_header_for_tool,
    schema_fingerprint,
)
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    CapabilitySpec,
    Permission,
    PluginDescriptor,
    PluginDescriptorStatus,
    PluginInstallation,
    PluginInstallationStatus,
)
from modules.integrations.catalog import CURATED_MCP_ENDPOINTS
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import PluginGatewayError
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class McpInstallationMixin(RuntimeCapabilityHost):
    """Own MCP-specific authorization, discovery, and persistence rules."""

    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    plugins: Repository[PluginDescriptor]
    secret_store: SecretStore

    def create_mcp_installation(
        self,
        *,
        display_name: str,
        endpoint_url: str,
        read_only_tool_names: tuple[str, ...],
        bearer_token: str | None = None,
        oauth_token_bundle: str | None = None,
        catalog_connector_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Install selected read-only tools from an allowlisted MCP server."""
        self._ensure_bootstrapped()
        principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(principal_id, Permission.PLUGINS_MANAGE)
        self._validate_connector_identity(catalog_connector_id, endpoint_url)
        credential = _access_token(bearer_token, oauth_token_bundle)
        self._validate_input(display_name, credential)
        selected = self._discover_selected_tools(
            endpoint_url, credential, read_only_tool_names
        )
        return self._persist_mcp_installation(
            display_name=display_name,
            endpoint_url=endpoint_url,
            selected=selected,
            bearer_token=bearer_token,
            oauth_token_bundle=oauth_token_bundle,
            catalog_connector_id=catalog_connector_id,
            principal_id=principal_id,
        )

    @staticmethod
    def _validate_connector_identity(
        connector_id: str | None, endpoint_url: str
    ) -> None:
        """Bind curated connector IDs to their canonical server endpoints."""
        if connector_id not in {"custom-mcp", *CURATED_MCP_ENDPOINTS}:
            raise ValueError("MCP connector ID is invalid")
        expected = CURATED_MCP_ENDPOINTS.get(connector_id or "")
        if expected and endpoint_url.rstrip("/") != expected.rstrip("/"):
            raise ValueError("Connector ID does not match its MCP endpoint")

    def _discover_selected_tools(
        self,
        endpoint_url: str,
        bearer_token: str | None,
        requested_names: tuple[str, ...],
    ) -> tuple[dict[str, Any], ...]:
        """Discover the server's tools and validate the requested read set."""
        try:
            discovered = self.mcp_plugin.discover_tools(
                endpoint_url, bearer_token.strip() if bearer_token else None
            )
            return self.mcp_plugin.validate_selected_tools(discovered, requested_names)
        except PluginGatewayError as exc:
            raise ValueError(exc.message) from exc

    def _persist_mcp_installation(
        self,
        *,
        display_name: str,
        endpoint_url: str,
        selected: tuple[dict[str, Any], ...],
        bearer_token: str | None,
        oauth_token_bundle: str | None,
        catalog_connector_id: str | None,
        principal_id: str,
    ) -> PluginInstallation:
        """Persist the MCP descriptor, installation, secret, and audit event."""
        installation_id = _id()
        plugin_id = f"mcp-{installation_id}"
        descriptor, tool_mapping = _build_descriptor(plugin_id, display_name, selected)
        stored_credential = oauth_token_bundle or bearer_token
        secret_ref = (
            self.secret_store.put(stored_credential.strip())
            if stored_credential
            else None
        )
        installation = _installation_record(
            installation_id=installation_id,
            plugin_id=plugin_id,
            display_name=display_name,
            endpoint_url=endpoint_url,
            tool_mapping=tool_mapping,
            secret_ref=secret_ref,
            bearer_token=bearer_token,
            oauth_token_bundle=oauth_token_bundle,
            catalog_connector_id=catalog_connector_id,
        )
        descriptor_created = False
        try:
            self.plugins.create(descriptor)
            descriptor_created = True
            self.installations.create(installation)
        except Exception:
            if descriptor_created:
                self.plugins.delete(plugin_id)
            if secret_ref:
                self.secret_store.delete(secret_ref)
            raise
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=installation.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=installation.workspace_id,
                actor_principal_id=principal_id,
                action="plugin_installation.created",
                target_type="plugin_installation",
                target_id=installation_id,
                summary="MCP server connected with selected read tools",
                request_id=installation_id,
                metadata={"plugin_id": plugin_id, "tool_count": len(tool_mapping)},
                created_at=_now(),
            )
        )
        return installation

    @staticmethod
    def _validate_input(display_name: str, credential: str | None) -> None:
        """Validate bounded display names and optional bearer credentials."""
        if not display_name.strip() or len(display_name) > 160:
            raise ValueError("MCP display name must contain 1 to 160 characters")
        if credential is not None and not credential.strip():
            raise ValueError("MCP credential must not be blank")


def _access_token(
    bearer_token: str | None, oauth_token_bundle: str | None
) -> str | None:
    """Select one MCP auth method and extract an OAuth access token."""
    if bearer_token and oauth_token_bundle:
        raise ValueError("choose one MCP authentication method")
    if not oauth_token_bundle:
        return bearer_token
    try:
        token_data = json.loads(oauth_token_bundle)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP OAuth token bundle is invalid") from exc
    if not isinstance(token_data, dict):
        raise ValueError("MCP OAuth token bundle is invalid")
    access_token = token_data.get("access_token")
    if (
        token_data.get("weaves_oauth") is not True
        or token_data.get("provider") != "mcp"
        or not isinstance(access_token, str)
    ):
        raise ValueError("MCP OAuth token bundle is invalid")
    return access_token


def _build_descriptor(
    plugin_id: str, display_name: str, selected: tuple[dict[str, Any], ...]
) -> tuple[PluginDescriptor, dict[str, JsonValue]]:
    """Convert selected remote tools into bounded capability definitions."""
    mapping: dict[str, JsonValue] = {}
    capabilities: list[CapabilitySpec] = []
    try:
        for tool in selected:
            capability_id = capability_id_for_tool(tool["name"])
            mapping[capability_id] = {
                "name": tool["name"],
                "query_header": query_header_for_tool(tool),
                "input_schema_hash": schema_fingerprint(tool),
            }
            capabilities.append(imported_capability(capability_id, tool))
        descriptor = PluginDescriptor(
            plugin_id=plugin_id,
            plugin_type="mcp",
            display_name=display_name,
            description=f"Allowlisted tools from {display_name}",
            capability_manifest=tuple(capabilities),
            status=PluginDescriptorStatus.ACTIVE,
        )
    except ValidationError as exc:
        raise ValueError("MCP server returned an invalid tool schema") from exc
    return descriptor, mapping


def _installation_record(
    *,
    installation_id: str,
    plugin_id: str,
    display_name: str,
    endpoint_url: str,
    tool_mapping: dict[str, JsonValue],
    secret_ref: str | None,
    bearer_token: str | None,
    oauth_token_bundle: str | None,
    catalog_connector_id: str | None,
) -> PluginInstallation:
    """Build the persisted installation without exposing credential values."""
    configuration: dict[str, JsonValue] = {
        "endpoint": endpoint_url.rstrip("/"),
        "tools": tool_mapping,
        "auth_method": (
            "mcp_oauth"
            if oauth_token_bundle
            else "mcp_bearer"
            if bearer_token
            else "none"
        ),
    }
    if catalog_connector_id:
        configuration["catalog_connector_id"] = catalog_connector_id
    now = _now()
    return PluginInstallation(
        plugin_installation_id=installation_id,
        org_id=effective_org_id(),
        workspace_id=effective_workspace_id(),
        plugin_id=plugin_id,
        display_name=display_name,
        auth_ref=secret_ref,
        configuration=configuration,
        enabled_capability_ids=tuple(tool_mapping),
        status=PluginInstallationStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )


__all__ = ["McpInstallationMixin"]
