"""OAuth-based provider installation use cases."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
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
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class OAuthInstallationHost(ABC):
    """Dependencies for storing OAuth-created connector installations."""

    _bootstrapped: bool
    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    plugins: Repository[PluginDescriptor]
    secret_store: SecretStore

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before creating an installation."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce connector-management permission in the current workspace."""
        raise NotImplementedError


class OAuthInstallationMixin(OAuthInstallationHost):
    """Store validated OAuth grants as scoped plugin installations."""

    def create_slack_oauth_installation(
        self,
        *,
        display_name: str,
        token_bundle: str,
        channels: tuple[str, ...],
        include_thread_replies: bool = False,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Store an encrypted Slack OAuth token bundle for a workspace."""
        if not self._bootstrapped:
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        if not channels or len(channels) > 50 or len(set(channels)) != len(channels):
            raise ValueError("configure between 1 and 50 unique Slack channels")
        if any(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) is None for item in channels):
            raise ValueError("Slack channels must be channel names or IDs")
        return self._create_external_context_installation(
            plugin_id="slack",
            display_name=display_name,
            credential=token_bundle,
            configuration={
                "auth_method": "slack_oauth_v2",
                "channels": list(channels),
                **({"include_thread_replies": True} if include_thread_replies else {}),
            },
            capability_ids=("slack.messages.search",),
            audit_metadata={
                "channel_count": len(channels),
                "auth_method": "slack_oauth_v2",
            },
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_jira_oauth_installation(
        self,
        *,
        display_name: str,
        token_bundle: str,
        site_url: str,
        cloud_id: str,
        project_keys: tuple[str, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Store Atlassian 3LO credentials bound to an authorized Jira cloud."""
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
        if not cloud_id or len(cloud_id) > 128:
            raise ValueError("Atlassian cloud ID is invalid")
        if (
            not project_keys
            or len(project_keys) > 50
            or len(set(project_keys)) != len(project_keys)
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
            credential=token_bundle,
            configuration={
                "auth_method": "atlassian_3lo",
                "site_url": normalized_url,
                "cloud_id": cloud_id,
                "project_keys": list(project_keys),
            },
            capability_ids=("jira.issues.search",),
            audit_metadata={
                "project_count": len(project_keys),
                "auth_method": "atlassian_3lo",
            },
            requested_by_principal_id=requested_by_principal_id,
        )

    def _create_external_context_installation(
        self,
        *,
        plugin_id: str,
        display_name: str,
        credential: str,
        configuration: dict[str, Any],
        capability_ids: tuple[str, ...],
        audit_metadata: dict[str, Any],
        requested_by_principal_id: str,
    ) -> PluginInstallation:
        descriptor = self.plugins.get(plugin_id)
        secret_ref = self.secret_store.put(credential.strip())
        installation_id = _id()
        installation = PluginInstallation(
            plugin_installation_id=installation_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_id=plugin_id,
            display_name=display_name,
            auth_ref=secret_ref,
            configuration=configuration,
            enabled_capability_ids=capability_ids,
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
                summary=f"{descriptor.display_name} installation configured",
                request_id=installation_id,
                metadata={"plugin_id": plugin_id, **audit_metadata},
                created_at=_now(),
            )
        )
        return installation
