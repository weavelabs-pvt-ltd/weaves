"""Manage installed organization integrations and credentials."""

from __future__ import annotations

import re
from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
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


class IntegrationInstallationMixin(RuntimeCapabilityHost):
    """Own integration installation operations."""

    def create_github_app_installation(
        self,
        *,
        display_name: str,
        github_installation_id: str,
        repositories: tuple[str, ...],
        account_login: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> PluginInstallation:
        """Create a GitHub App connection using ephemeral installation tokens."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.PLUGINS_MANAGE)
        self._validate_github_installation_scope(github_installation_id, repositories)
        matches = [
            item
            for item in self.installations.list_scoped(
                effective_org_id(), effective_workspace_id()
            )
            if item.plugin_id == "github"
            and item.configuration.get("github_installation_id")
            == github_installation_id
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
        now = _now()
        installation = self._github_installation_record(
            display_name=display_name,
            github_installation_id=github_installation_id,
            repositories=repositories,
            account_login=account_login,
            existing=existing,
            installation_id=installation_id,
            now=now,
        )
        self.installations.put(installation) if existing else self.installations.create(
            installation
        )
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
                    "GitHub App connection scope updated"
                    if existing
                    else "GitHub App installation connected"
                ),
                request_id=installation_id,
                metadata={"plugin_id": "github", "repository_count": len(repositories)},
                created_at=now,
            )
        )
        self._disable_duplicate_github_app_installations(
            [
                item
                for item in matches
                if item.plugin_installation_id != installation_id
            ],
            requested_by_principal_id,
            now,
        )
        return installation

    @staticmethod
    def _validate_github_installation_scope(
        github_installation_id: str, repositories: tuple[str, ...]
    ) -> None:
        """Validate the GitHub installation identity and selected repositories."""
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

    def _github_installation_record(
        self,
        *,
        display_name: str,
        github_installation_id: str,
        repositories: tuple[str, ...],
        account_login: str | None,
        existing: PluginInstallation | None,
        installation_id: str,
        now: Any,
    ) -> PluginInstallation:
        """Build the canonical record for a GitHub App installation."""
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
                **({"account_login": account_login} if account_login else {}),
            },
            enabled_capability_ids=(
                tuple(
                    dict.fromkeys(
                        (*existing.enabled_capability_ids, "github.code.search")
                    )
                )
                if existing
                else ("github.issues.search", "github.code.search")
            ),
            status=(
                PluginInstallationStatus.DISABLED
                if existing and existing.status is PluginInstallationStatus.DISABLED
                else PluginInstallationStatus.ACTIVE
            ),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        return installation

    def _disable_duplicate_github_app_installations(
        self, duplicates: list[PluginInstallation], actor_id: str, timestamp: Any
    ) -> None:
        """Disable legacy duplicate records for the same GitHub App install."""
        for duplicate in duplicates:
            if duplicate.status is PluginInstallationStatus.DISABLED:
                continue
            disabled = duplicate.model_copy(
                update={
                    "status": PluginInstallationStatus.DISABLED,
                    "updated_at": timestamp,
                }
            )
            self.installations.put(disabled)
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
                    summary="Duplicate GitHub App connection disabled",
                    request_id=duplicate.plugin_installation_id,
                    metadata={
                        "plugin_id": "github",
                        "reason": "duplicate_installation",
                    },
                    created_at=timestamp,
                )
            )
