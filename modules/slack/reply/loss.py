"""End an installation whose bot token Slack says is dead (worker side).

The API process does this through ``SlackInstallationService.mark_lost``; the
worker has no signing secret and no install settings, so it carries only what the
loss needs: mark the row revoked, drop the token from the secret store (which
writes the tombstone) and audit it. Events can be missed while the endpoint is
down, so a failed post with a token error is the second place we learn of it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from application.ports.slack import HELD_STATUSES, SlackInstallation, SlackStore
from contracts.v1 import AuditEvent, AuditTargetScope
from shared.clock import new_id, utc_now

logger = logging.getLogger("product.slack")


class InstallationLoss:
    """Mark an installation revoked after a dead-token answer from Slack."""

    def __init__(
        self,
        *,
        store: SlackStore,
        secrets: SecretStore,
        audit_events: Repository[AuditEvent],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the store, secret store and audit repository."""
        self._store = store
        self._secrets = secrets
        self._audit = audit_events
        self._clock = clock

    def __call__(self, installation: SlackInstallation) -> None:
        """Revoke the row, delete its token and audit it; safe to repeat."""
        if installation.status not in HELD_STATUSES:
            return
        now = self._clock()
        updated = self._store.set_status(
            installation.installation_id, "revoked", now, clear_secret=True
        )
        if updated is None:
            return
        if installation.bot_secret_ref is not None:
            try:
                self._secrets.delete(installation.bot_secret_ref)
            except Exception as error:
                logger.error("slack_secret_delete_failed kind=%s", type(error).__name__)
        event_id = new_id()
        self._audit.create(
            AuditEvent(
                audit_event_id=event_id,
                org_id=installation.org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=f"slack:{installation.team_id}",
                action="slack.installation.revoked",
                target_type="slack_installation",
                target_id=installation.installation_id,
                summary="Slack bot revoked",
                request_id=event_id,
                metadata={"team_id": installation.team_id, "cause": "token_error"},
                created_at=now,
            )
        )
