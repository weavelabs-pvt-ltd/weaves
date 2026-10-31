"""Integration administration and connection health use cases."""

from __future__ import annotations

from collections.abc import Callable

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from clients.integrations.github import GitHubReadPlugin
from clients.integrations.jira import JiraReadPlugin
from clients.integrations.mcp import McpHttpPlugin
from clients.integrations.slack import SlackReadPlugin
from contracts.v1 import (
    AuditEvent,
    CapabilityKind,
    CapabilitySpec,
    KnowledgeDocument,
    Permission,
    PluginDescriptor,
    PluginDescriptorStatus,
    PluginInstallation,
    PluginInstallationStatus,
)
from modules.integrations.runtime_installation_admin import (
    IntegrationInstallationAdminMixin,
)
from modules.integrations.runtime_installation_health import (
    IntegrationInstallationHealthMixin,
)
from shared.request_context import effective_org_id, effective_workspace_id


class IntegrationAdminService(
    IntegrationInstallationAdminMixin, IntegrationInstallationHealthMixin
):
    """Manage installation configuration, credentials, and health checks."""

    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    plugins: Repository[PluginDescriptor]
    knowledge_documents: Repository[KnowledgeDocument]
    secret_store: SecretStore
    github_plugin: GitHubReadPlugin
    slack_plugin: SlackReadPlugin
    jira_plugin: JiraReadPlugin
    mcp_plugin: McpHttpPlugin

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        installations: Repository[PluginInstallation],
        plugins: Repository[PluginDescriptor],
        knowledge_documents: Repository[KnowledgeDocument],
        secret_store: SecretStore,
        github_plugin: GitHubReadPlugin,
        slack_plugin: SlackReadPlugin,
        jira_plugin: JiraReadPlugin,
        mcp_plugin: McpHttpPlugin,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
    ) -> None:
        """Bind repositories, secret storage, connector clients, and auth ports."""
        self.audit_events = audit_events
        self.installations = installations
        self.plugins = plugins
        self.knowledge_documents = knowledge_documents
        self.secret_store = secret_store
        self.github_plugin = github_plugin
        self.slack_plugin = slack_plugin
        self.jira_plugin = jira_plugin
        self.mcp_plugin = mcp_plugin
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize

    def _ensure_bootstrapped(self) -> None:
        """Initialize product defaults before running an admin use case."""
        if not self._is_bootstrapped():
            self._bootstrap()

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Check permissions using the configured identity capability."""
        self._authorize(principal_id, permission)

    def list_plugin_descriptors(self) -> tuple[PluginDescriptor, ...]:
        """List registered connector definitions for authorized workspace users."""
        self._ensure_bootstrapped()
        self.authorize("local-developer", Permission.PLUGINS_MANAGE)
        return self.plugins.list()

    def list_plugin_installations(self) -> tuple[PluginInstallation, ...]:
        """List installations visible in the selected workspace."""
        self._ensure_bootstrapped()
        self.authorize("local-developer", Permission.PLUGINS_MANAGE)
        return self.installations.list_scoped(
            effective_org_id(), effective_workspace_id()
        )

    def resolve_capability_installation(
        self,
        capability_id: str,
        *,
        org_id: str,
        workspace_id: str,
        require_enabled: bool,
        allowed_kinds: tuple[CapabilityKind, ...] = (CapabilityKind.CONTEXT_READ,),
    ) -> tuple[PluginInstallation, CapabilitySpec]:
        """Find the unique installation that exposes an allowed capability."""
        matches: list[tuple[PluginInstallation, CapabilitySpec]] = []
        for installation in self.installations.list_scoped(org_id, workspace_id):
            match = self._match_installation_capability(
                installation, capability_id, require_enabled, allowed_kinds
            )
            if match is not None:
                matches.append(match)
        if not matches:
            raise ValueError(
                f"capability {capability_id} has no active workspace installation"
            )
        if len(matches) > 1:
            raise ValueError(
                f"capability {capability_id} is enabled by multiple installations"
            )
        return matches[0]

    def _match_installation_capability(
        self,
        installation: PluginInstallation,
        capability_id: str,
        require_enabled: bool,
        allowed_kinds: tuple[CapabilityKind, ...],
    ) -> tuple[PluginInstallation, CapabilitySpec] | None:
        """Return a capability match when this installation satisfies policy."""
        try:
            descriptor = self.plugins.get(installation.plugin_id)
        except KeyError:
            return None
        capability = next(
            (
                item
                for item in descriptor.capability_manifest
                if item.capability_id == capability_id
            ),
            None,
        )
        if capability is None or not self._capability_is_allowed(
            installation, descriptor, capability, capability_id, require_enabled
        ):
            return None
        if capability.kind not in allowed_kinds:
            return None
        return installation, capability

    @staticmethod
    def _capability_is_allowed(
        installation: PluginInstallation,
        descriptor: PluginDescriptor,
        capability: CapabilitySpec,
        capability_id: str,
        require_enabled: bool,
    ) -> bool:
        """Check approval support, installation status, and plugin status."""
        if (
            capability.kind is CapabilityKind.ACTION_WRITE
            and not capability.approval_supported
        ):
            return False
        return not require_enabled or (
            installation.status is PluginInstallationStatus.ACTIVE
            and capability_id in installation.enabled_capability_ids
            and descriptor.status is PluginDescriptorStatus.ACTIVE
        )
