"""The ``:eyes:`` acknowledgement on an ``app_mention`` (design scene A).

Runs after the event is acked, with the installation's bot token. It never
blocks or fails the ack: an error is logged by Slack error code only (no text,
no token) and a dead token marks the installation revoked. ``already_reacted``
is success, so a retry or a second mention of the same message is harmless.
"""

from __future__ import annotations

import logging

from application.ports.secrets import SecretStore
from application.ports.slack import SlackInstallation
from clients.slack.client import SlackApiClient
from clients.slack.errors import SlackApiError, SlackTokenError
from modules.slack.events import tenant_of
from modules.slack.installation_service import SlackInstallationService

logger = logging.getLogger("product.slack")
ACK_EMOJI = "eyes"


class AckReaction:
    """Add the acknowledgement reaction for one accepted mention."""

    def __init__(
        self,
        api: SlackApiClient,
        secrets: SecretStore,
        installs: SlackInstallationService,
    ) -> None:
        """Bind the client, the secret store and the loss handler."""
        self._api = api
        self._secrets = secrets
        self._installs = installs

    def __call__(
        self, installation: SlackInstallation, channel: str, message_ts: str
    ) -> bool:
        """Add the reaction; returns whether Slack accepted it as new."""
        if installation.bot_secret_ref is None:
            return False
        try:
            with tenant_of(installation):
                token = self._secrets.get(installation.bot_secret_ref)
                return self._api.add_reaction(token, channel, message_ts, ACK_EMOJI)
        except SlackTokenError as error:
            logger.warning(
                "slack_ack_reaction_token team=%s code=%s",
                installation.team_id,
                error.code,
            )
            self._lose(installation)
        except SlackApiError as error:
            logger.warning(
                "slack_ack_reaction_failed team=%s code=%s",
                installation.team_id,
                error.code,
            )
        except Exception as error:
            logger.warning(
                "slack_ack_reaction_error team=%s kind=%s",
                installation.team_id,
                type(error).__name__,
            )
        return False

    def _lose(self, installation: SlackInstallation) -> None:
        try:
            with tenant_of(installation):
                self._installs.mark_lost(
                    installation, "revoked", actor=f"slack:{installation.team_id}"
                )
        except Exception as error:
            logger.error("slack_mark_lost_failed kind=%s", type(error).__name__)
