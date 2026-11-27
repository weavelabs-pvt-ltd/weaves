"""The Slack bot install: start, status, disconnect and loss of an installation.

A signed-in organization admin starts the install, so the organization is chosen
by Weaves and never inferred from Slack. The bot token is stored only in the
encrypted secret store (purpose ``slack_bot_token``). Ending an installation
(Disconnect, uninstall, revocation) is one shared path in
``modules.slack.lifecycle``. Nothing here logs a token or a code.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from application.ports.secrets import SecretStore
from application.ports.slack import SlackInstallation, SlackStore
from clients.slack.client import SlackApiClient
from contracts.v1 import Permission
from modules.auth.credential_service import CredentialService
from modules.integrations.oauth_state_service import OAuthStateService
from modules.slack.install_settings import (
    AUTHORIZE_URL,
    BOT_SCOPES,
    SlackInstallSettings,
)
from modules.slack.lifecycle import EndStatus, SlackLifecycle
from shared.request_context import effective_principal_id

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
        lifecycle: SlackLifecycle,
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
        self.lifecycle = lifecycle
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
        self.lifecycle.audit(installation, actor, action, extra)

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
        """Stop the bot for one workspace (shared path: tombstone, flip, revoke)."""
        actor = self.require_admin()
        row = self.store.get_for_org(installation_id)
        if row is None:
            raise InstallationNotFound(installation_id)
        ended = self.lifecycle.end(
            row,
            "uninstalled",
            revoke=True,
            actor=actor,
            action="slack.installation.disconnected",
        )
        return self._view(ended or row)

    def retire_token(self, row: SlackInstallation, *, revoke: bool) -> None:
        """Delete the previous token of a reinstall (best effort revoke first)."""
        self.lifecycle.retire_token(row, revoke=revoke)

    # -- loss (Slack told us the token is gone) ------------------------------

    def mark_lost(
        self, row: SlackInstallation, status: EndStatus, *, actor: str = "slack"
    ) -> SlackInstallation | None:
        """Slack told us the bot is gone: end ``row`` without calling Slack."""
        return self.lifecycle.end(row, status, revoke=False, actor=actor)
