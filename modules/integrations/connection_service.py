"""Create connector installations through explicit application ports."""

from __future__ import annotations

from collections.abc import Callable

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from clients.integrations.mcp import McpHttpPlugin
from contracts.v1 import (
    AuditEvent,
    Permission,
    PluginDescriptor,
    PluginInstallation,
)
from modules.integrations.oauth_installations import OAuthInstallationMixin
from modules.integrations.runtime_installations import IntegrationInstallationMixin
from modules.integrations.runtime_mcp_installations import McpInstallationMixin


class IntegrationConnectionService(
    OAuthInstallationMixin, McpInstallationMixin, IntegrationInstallationMixin
):
    """Create token, OAuth, GitHub App, and MCP installations."""

    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    plugins: Repository[PluginDescriptor]
    secret_store: SecretStore
    mcp_plugin: McpHttpPlugin

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        installations: Repository[PluginInstallation],
        plugins: Repository[PluginDescriptor],
        secret_store: SecretStore,
        mcp_plugin: McpHttpPlugin,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
    ) -> None:
        """Bind persistence, secret storage, MCP discovery, and auth ports."""
        self.audit_events = audit_events
        self.installations = installations
        self.plugins = plugins
        self.secret_store = secret_store
        self.mcp_plugin = mcp_plugin
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize

    def _ensure_bootstrapped(self) -> None:
        """Initialize product defaults before creating an installation."""
        if not self._is_bootstrapped():
            self._bootstrap()

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce connector management permission for the active workspace."""
        self._authorize(principal_id, permission)
