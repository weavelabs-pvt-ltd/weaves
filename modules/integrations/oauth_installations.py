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
    def _ensure_bootstrapped(self) -> None:
        """Ensure platform bootstrap has completed before installation writes."""
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
        team_id: str | None = None,
        team_name: str | None = None,
        include_thread_replies: bool = False,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Store an encrypted Slack OAuth token bundle for a workspace."""
        self._ensure_bootstrapped()
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
                **({"team_id": team_id} if team_id else {}),
                **({"team_name": team_name} if team_name else {}),
                **({"include_thread_replies": True} if include_thread_replies else {}),
            },
            capability_ids=("slack.messages.search",),
            audit_metadata={
                "channel_count": len(channels),
                "auth_method": "slack_oauth_v2",
            },
            requested_by_principal_id=requested_by_principal_id,
            identity_field="team_id",
            identity_value=team_id,
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
        self._ensure_bootstrapped()
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
            identity_field="cloud_id",
            identity_value=cloud_id,
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
        identity_field: str | None = None,
        identity_value: str | None = None,
    ) -> PluginInstallation:
        descriptor = self.plugins.get(plugin_id)
        secret_ref = self.secret_store.put(credential.strip())
        matches = [
            item
            for item in self.installations.list_scoped(
                effective_org_id(), effective_workspace_id()
            )
            if item.plugin_id == plugin_id
            and identity_field
            and item.configuration.get(identity_field) == identity_value
        ]
        existing = next(
            (
                item
                for item in matches
                if item.status is not PluginInstallationStatus.DISABLED
            ),
            matches[0] if matches else None,
        )
        installation_id = existing.plugin_installation_id if existing else _id()
        installation = PluginInstallation(
            plugin_installation_id=installation_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_id=plugin_id,
            display_name=display_name,
            auth_ref=secret_ref,
            configuration=configuration,
            enabled_capability_ids=(
                existing.enabled_capability_ids if existing else capability_ids
            ),
            status=(
                PluginInstallationStatus.DISABLED
                if existing and existing.status == PluginInstallationStatus.DISABLED
                else PluginInstallationStatus.ACTIVE
            ),
            created_at=existing.created_at if existing else _now(),
            updated_at=_now(),
        )
        try:
            self.installations.put(
                installation
            ) if existing else self.installations.create(installation)
        except Exception:
            self.secret_store.delete(secret_ref)
            raise
        self._audit_external_context_installation(
            installation=installation,
            descriptor=descriptor,
            plugin_id=plugin_id,
            audit_metadata=audit_metadata,
            requested_by_principal_id=requested_by_principal_id,
            existing=existing,
        )
        if existing and existing.auth_ref and existing.auth_ref != secret_ref:
            self.secret_store.delete(existing.auth_ref)
        self._disable_duplicate_external_installations(
            [
                item
                for item in matches
                if item.plugin_installation_id != installation_id
            ],
            plugin_id,
            requested_by_principal_id,
        )
        return installation

    def _audit_external_context_installation(
        self,
        *,
        installation: PluginInstallation,
        descriptor: PluginDescriptor,
        plugin_id: str,
        audit_metadata: dict[str, Any],
        requested_by_principal_id: str,
        existing: PluginInstallation | None,
    ) -> None:
        """Record a safe audit event for OAuth connector creation or update."""
        installation_id = installation.plugin_installation_id
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=installation.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=installation.workspace_id,
                actor_principal_id=requested_by_principal_id,
                action=(
                    "plugin_installation.updated"
                    if existing
                    else "plugin_installation.created"
                ),
                target_type="plugin_installation",
                target_id=installation_id,
                summary=(
                    f"{descriptor.display_name} connection scope updated"
                    if existing
                    else f"{descriptor.display_name} installation configured"
                ),
                request_id=installation_id,
                metadata={"plugin_id": plugin_id, **audit_metadata},
                created_at=_now(),
            )
        )

    def _disable_duplicate_external_installations(
        self,
        duplicates: list[PluginInstallation],
        plugin_id: str,
        actor_id: str,
    ) -> None:
        """Disable legacy duplicate records for a provider account in this workspace."""
        for duplicate in duplicates:
            if duplicate.status is PluginInstallationStatus.DISABLED:
                continue
            now = _now()
            self.installations.put(
                duplicate.model_copy(
                    update={
                        "status": PluginInstallationStatus.DISABLED,
                        "updated_at": now,
                    }
                )
            )
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=duplicate.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=duplicate.workspace_id,
                    actor_principal_id=actor_id,
                    action="plugin_installation.disabled",
                    target_type="plugin_installation",
                    target_id=duplicate.plugin_installation_id,
                    summary=f"Duplicate {plugin_id} connection disabled",
                    request_id=duplicate.plugin_installation_id,
                    metadata={"plugin_id": plugin_id, "reason": "duplicate_account"},
                    created_at=now,
                )
            )
