"""The Slack bot install: start, status, disconnect and loss of an installation.

A signed-in organization admin starts the install, so the organization is chosen
by Weaves and never inferred from Slack. The bot token is stored only in the
encrypted secret store (purpose ``slack_bot_token``); deleting it writes the
6.2 tombstone through the store decorator. Nothing here logs a token or a code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from application.ports.secrets import SecretStore
from application.ports.slack import HELD_STATUSES, SlackInstallation, SlackStore
from clients.slack.client import SlackApiClient
from contracts.v1 import AuditEvent, AuditTargetScope, Permission
from modules.auth.credential_service import CredentialService
from modules.integrations.oauth_state_service import OAuthStateService
from modules.slack.install_settings import (
    AUTHORIZE_URL,
    BOT_SCOPES,
    SlackInstallSettings,
)
from shared.clock import new_id
from shared.request_context import effective_principal_id

logger = logging.getLogger("product.slack")
SECRET_PURPOSE = "slack_bot_token"


class SlackUnavailable(Exception):
    """The install flow is not configured on this deployment."""


class InstallationNotFound(Exception):
    """No such installation in the caller's organization."""


class SlackInstallationService:
    """Install, report on and remove the bot for the caller's organization."""

    def __init__(
        self,
        *,
        store: SlackStore,
        secrets: SecretStore,
        state_service: OAuthStateService,
        credentials: CredentialService,
        api: SlackApiClient,
        settings: SlackInstallSettings,
        display_name: Callable[[str], str | None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind ports and settings; the API client is injected for tests."""
        self.store = store
        self.secrets = secrets
        self.state_service = state_service
        self.credentials = credentials
        self.api = api
        self.settings = settings
        self.display_name = display_name
        self.clock = clock

    # -- authorization and audit -----------------------------------------

    def require_admin(self) -> str:
        """The signed-in principal, which must hold the organization admin role."""
        principal = self.credentials.authenticate_principal(
            effective_principal_id("local-developer")
        )
        self.credentials.authorize_organization(
            principal, Permission.ORGANIZATIONS_ADMIN
        )
        return principal

    def audit(
        self,
        org_id: str,
        actor: str,
        action: str,
        installation: SlackInstallation,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Write one organization audit event; ids and counts only, no secrets."""
        metadata: dict[str, Any] = {
            "team_id": installation.team_id,
            "status": installation.status,
            **(extra or {}),
        }
        self.credentials.audit_events.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type="slack_installation",
                target_id=installation.installation_id,
                summary=f"Slack bot {action.rsplit('.', 1)[-1]}",
                request_id=new_id(),
                metadata=metadata,
                created_at=self.clock(),
            )
        )

    # -- start ---------------------------------------------------------------

    def start(self) -> dict[str, str]:
        """Return the Slack authorize URL for the signed-in admin's organization."""
        if not self.settings.install_available:
            raise SlackUnavailable("Slack install is not configured")
        self.require_admin()
        state = self.state_service.create_state("slack_bot", {"flow": "bot_install"})
        query = urlencode(
            {
                "client_id": self.settings.client_id,
                "scope": ",".join(BOT_SCOPES),
                "redirect_uri": self.settings.redirect_uri,
                "state": state.oauth_state_id,
            }
        )
        return {"authorization_url": f"{AUTHORIZE_URL}?{query}"}

    # -- status ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What the admin card shows: availability and this organization's rows."""
        self.require_admin()
        rows = self.store.list_for_org()
        return {
            "available": self.settings.install_available,
            "installations": [self._view(row) for row in rows],
        }

    def _view(self, row: SlackInstallation) -> dict[str, Any]:
        missing = [s for s in BOT_SCOPES if s not in row.scopes]
        return {
            "installation_id": row.installation_id,
            "team_id": row.team_id,
            "team_name": row.team_name,
            "status": row.status,
            "installed_by": row.installed_by_name,
            "installed_at": row.installed_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "missing_scopes": missing,
            "needs_reinstall": row.status == "revoked" or bool(missing),
        }

    # -- disconnect ------------------------------------------------------------

    def disconnect(self, installation_id: str) -> dict[str, Any]:
        """Stop the bot for one workspace: mark it, then revoke and delete the token.

        The row is changed first so dispatch stops at once; the Slack revoke is
        best effort and the secret deletion writes the tombstone.
        """
        actor = self.require_admin()
        row = self.store.get_for_org(installation_id)
        if row is None:
            raise InstallationNotFound(installation_id)
        if row.status == "uninstalled":
            return self._view(row)
        updated = self.store.set_status(
            row.installation_id, "uninstalled", self.clock(), clear_secret=True
        )
        assert updated is not None
        self.retire_token(row, revoke=True)
        self.audit(row.org_id, actor, "slack.installation.disconnected", updated)
        return self._view(updated)

    def retire_token(self, row: SlackInstallation, *, revoke: bool) -> None:
        """Revoke (best effort) and delete the stored bot token of ``row``."""
        ref = row.bot_secret_ref
        if ref is None:
            return
        if revoke:
            try:
                self.api.auth_revoke(self.secrets.get(ref))
            except Exception as error:
                logger.warning(
                    "slack_revoke_failed team=%s kind=%s",
                    row.team_id,
                    type(error).__name__,
                )
        try:
            self.secrets.delete(ref)
        except Exception as error:
            # The tombstone is written first; a refusal leaves a tenant-owned
            # secret that the organization purge removes.
            logger.error(
                "slack_secret_delete_failed team=%s kind=%s",
                row.team_id,
                type(error).__name__,
            )

    # -- loss (Slack told us the token is gone) ------------------------------

    def mark_lost(
        self, row: SlackInstallation, status: str, *, actor: str = "slack"
    ) -> SlackInstallation | None:
        """Mark ``row`` revoked or uninstalled and delete the dead token.

        Run under the installation's organization scope. Events can repeat, so a
        row that is already out of use is left alone.
        """
        if row.status not in HELD_STATUSES:
            return None
        assert status in {"revoked", "uninstalled"}
        updated = self.store.set_status(
            row.installation_id,
            "revoked" if status == "revoked" else "uninstalled",
            self.clock(),
            clear_secret=True,
        )
        if updated is None:
            return None
        self.retire_token(row, revoke=False)
        self.audit(row.org_id, actor, f"slack.installation.{status}", updated)
        return updated
