"""Manage installed organization integrations and credentials."""

from __future__ import annotations

import re
from typing import Any

from pydantic import JsonValue, ValidationError

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
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import PluginGatewayError
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class IntegrationInstallationMixin(RuntimeCapabilityHost):
    """Own integration installation operations."""

    def create_github_installation(
        self,
        *,
        display_name: str,
        api_token: str,
        repositories: tuple[str, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Connect GitHub with repository-scoped read-only configuration."""
        if not self._bootstrapped:
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        if not api_token.strip():
            raise ValueError("GitHub API token is required")
        if not repositories or len(repositories) > 50:
            raise ValueError("configure between 1 and 50 repositories")
        if len(repositories) != len(set(repositories)):
            raise ValueError("repositories must be unique")
        if any(
            len(repo) > 100
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None
            for repo in repositories
        ):
            raise ValueError("repositories must use the owner/repository format")
        descriptor = self.plugins.get("github")
        secret_ref = self.secret_store.put(api_token.strip())
        installation_id = _id()
        installation = PluginInstallation(
            plugin_installation_id=installation_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_id=descriptor.plugin_id,
            display_name=display_name,
            auth_ref=secret_ref,
            configuration={"repositories": list(repositories)},
            enabled_capability_ids=("github.issues.search",),
            status=PluginInstallationStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
        )
        try:
            self.installations.create(installation)
        except Exception:
            self.secret_store.delete(secret_ref)
            raise
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=installation.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=installation.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="plugin_installation.created",
                target_type="plugin_installation",
                target_id=installation_id,
                summary="GitHub installation configured",
                request_id=installation_id,
                metadata={
                    "plugin_id": "github",
                    "repository_count": len(repositories),
                },
                created_at=_now(),
            )
        )
        return installation

    def create_github_app_installation(
        self,
        *,
        display_name: str,
        github_installation_id: str,
        repositories: tuple[str, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Create a GitHub App connection using ephemeral installation tokens."""
        if not self._bootstrapped:
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        if not github_installation_id.isdigit():
            raise ValueError("GitHub installation ID is invalid")
        if (
            not repositories
            or len(repositories) > 50
            or len(set(repositories)) != len(repositories)
        ):
            raise ValueError("configure between 1 and 50 unique repositories")
        if any(
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item) is None
            for item in repositories
        ):
            raise ValueError("repositories must use the owner/repository format")
        installation_id = _id()
        now = _now()
        installation = PluginInstallation(
            plugin_installation_id=installation_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_id=self.plugins.get("github").plugin_id,
            display_name=display_name,
            auth_ref=None,
            configuration={
                "auth_method": "github_app",
                "github_installation_id": github_installation_id,
                "repositories": list(repositories),
            },
            enabled_capability_ids=("github.issues.search",),
            status=PluginInstallationStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self.installations.create(installation)
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=installation.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=installation.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action="plugin_installation.created",
                target_type="plugin_installation",
                target_id=installation_id,
                summary="GitHub App installation connected",
                request_id=installation_id,
                metadata={"plugin_id": "github", "repository_count": len(repositories)},
                created_at=now,
            )
        )
        return installation

    def create_slack_installation(
        self,
        *,
        display_name: str,
        user_token: str,
        channels: tuple[str, ...],
        include_thread_replies: bool = False,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Connect a Slack user token, limited to selected channel names."""
        if not self._bootstrapped:
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        if not user_token.strip():
            raise ValueError("Slack user token is required")
        if not channels or len(channels) > 50 or len(channels) != len(set(channels)):
            raise ValueError("configure between 1 and 50 unique Slack channels")
        if any(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) is None for item in channels):
            raise ValueError(
                "Slack channels must use channel names without the # prefix"
            )
        return self._create_external_context_installation(
            plugin_id="slack",
            display_name=display_name,
            credential=user_token,
            configuration={
                "channels": list(channels),
                **({"include_thread_replies": True} if include_thread_replies else {}),
            },
            capability_ids=("slack.messages.search",),
            audit_metadata={"channel_count": len(channels)},
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_jira_installation(
        self,
        *,
        display_name: str,
        api_token: str,
        site_url: str,
        email: str,
        project_keys: tuple[str, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Connect Jira Cloud using a token scoped to selected project keys."""
        if not self._bootstrapped:
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        normalized_url = site_url.strip().rstrip("/")
        if (
            re.fullmatch(r"https://[A-Za-z0-9-]+\.atlassian\.net", normalized_url)
            is None
        ):
            raise ValueError("Jira site must be an https://<site>.atlassian.net URL")
        if not email.strip() or "@" not in email or len(email) > 320:
            raise ValueError("a valid Jira account email is required")
        if not api_token.strip():
            raise ValueError("Jira API token is required")
        if (
            not project_keys
            or len(project_keys) > 50
            or len(project_keys) != len(set(project_keys))
        ):
            raise ValueError("configure between 1 and 50 unique Jira projects")
        if any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", item) is None for item in project_keys
        ):
            raise ValueError(
                "Jira project keys must use uppercase letters, digits, and underscores"
            )
        return self._create_external_context_installation(
            plugin_id="jira",
            display_name=display_name,
            credential=api_token,
            configuration={
                "site_url": normalized_url,
                "email": email.strip(),
                "project_keys": list(project_keys),
            },
            capability_ids=("jira.issues.search",),
            audit_metadata={"project_count": len(project_keys)},
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_mcp_installation(
        self,
        *,
        display_name: str,
        endpoint_url: str,
        read_only_tool_names: tuple[str, ...],
        bearer_token: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Import explicitly allowlisted query tools from an MCP server."""
        if not self._bootstrapped:
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        self._validate_mcp_installation_input(display_name, bearer_token)
        selected = self._discover_selected_mcp_tools(
            endpoint_url, bearer_token, read_only_tool_names
        )

        installation_id = _id()
        plugin_id = f"mcp-{installation_id}"
        descriptor, capability_mapping = self._build_mcp_plugin_descriptor(
            plugin_id, display_name, selected
        )
        secret_ref = (
            self.secret_store.put(bearer_token.strip())
            if bearer_token and bearer_token.strip()
            else None
        )
        now = _now()
        installation = PluginInstallation(
            plugin_installation_id=installation_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_id=plugin_id,
            display_name=display_name,
            auth_ref=secret_ref,
            configuration={
                "endpoint": endpoint_url.rstrip("/"),
                "tools": capability_mapping,
            },
            enabled_capability_ids=tuple(capability_mapping),
            status=PluginInstallationStatus.ACTIVE,
            created_at=now,
            updated_at=now,
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
                actor_principal_id=requested_by_principal_id,
                action="plugin_installation.created",
                target_type="plugin_installation",
                target_id=installation_id,
                summary="MCP server connected with selected read tools",
                request_id=installation_id,
                metadata={
                    "plugin_id": plugin_id,
                    "tool_count": len(capability_mapping),
                },
                created_at=now,
            )
        )
        return installation

    @staticmethod
    def _validate_mcp_installation_input(
        display_name: str, bearer_token: str | None
    ) -> None:
        """Validate the user-provided MCP display name and optional token."""
        if not display_name.strip():
            raise ValueError("MCP display name is required")
        if len(display_name) > 160:
            raise ValueError("MCP display name must be 160 characters or less")
        if bearer_token is not None and not bearer_token.strip():
            raise ValueError("MCP bearer token must not be blank")

    def _discover_selected_mcp_tools(
        self,
        endpoint_url: str,
        bearer_token: str | None,
        requested_tool_names: tuple[str, ...],
    ) -> tuple[dict[str, Any], ...]:
        """Discover tools and restrict the installation to requested names."""
        try:
            discovered = self.mcp_plugin.discover_tools(
                endpoint_url, bearer_token.strip() if bearer_token else None
            )
            return self.mcp_plugin.validate_selected_tools(
                discovered, requested_tool_names
            )
        except PluginGatewayError as exc:
            raise ValueError(exc.message) from exc

    @staticmethod
    def _build_mcp_plugin_descriptor(
        plugin_id: str, display_name: str, selected: tuple[dict[str, Any], ...]
    ) -> tuple[PluginDescriptor, dict[str, JsonValue]]:
        """Build the imported plugin definition and capability settings."""
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
