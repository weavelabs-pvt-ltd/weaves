"""One code path that ends, suspends and restores Slack installations.

Every way a workspace stops being served runs through :meth:`SlackLifecycle.end`:
the Weaves-side Disconnect, ``app_uninstalled``, a bot ``tokens_revoked``, a
dead-token reply failure, a quarantine that revokes credentials and the restore
reconcile. Order matters and is fail closed:

1. the 6.2 tombstone for this installation generation (refused if unwritable);
2. the status flips, so dispatch stops at once and the unique ``team_id`` is
   freed for a clean reinstall by the same or another organization;
3. the team's pending inbox rows are dropped (their text is erased);
4. ``auth.revoke`` (best effort, only when Weaves ends it), then the secret
   delete, whose store decorator writes the secret tombstone first;
5. the token reference is cleared and one audit event is written.

A crash after step 2 leaves an ended row that still names its token;
:meth:`sweep` finishes it, so the sequence converges. Nothing here logs or
audits a token, a code or a message.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from application.ports.repository import Repository
from application.ports.secrets import SecretStore
from application.ports.slack import HELD_STATUSES, SlackInstallation, SlackStore
from clients.slack.client import SlackApiClient
from contracts.v1 import AuditEvent, AuditTargetScope
from modules.recovery.tombstones import (
    SLACK_INSTALLATION_ENDED,
    NullTombstones,
    TombstoneSink,
)
from shared.clock import new_id
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack")
EndStatus = Literal["revoked", "uninstalled"]
DROP_REASON_ENDED = "installation_ended"


def generation(row: SlackInstallation) -> str:
    """Identify one install of a workspace (a reinstall starts a new one)."""
    stamp = row.installed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{row.installation_id}@{stamp}"


def tenant_of(row: SlackInstallation) -> Any:
    """Bind the installation's own organization; returns the reset token."""
    return set_request_identity(
        row.installed_by_principal_id, row.org_id, row.workspace_id
    )


class SlackLifecycle:
    """End, retire, suspend and restore installations; sweep what a crash left."""

    def __init__(
        self,
        *,
        store: SlackStore,
        secrets: SecretStore,
        api: SlackApiClient,
        audit_events: Repository[AuditEvent],
        tombstones: TombstoneSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind ports; the tombstone sink defaults to the no-op sink."""
        self.store = store
        self.secrets = secrets
        self.api = api
        self.audit_events = audit_events
        self.tombstones: TombstoneSink = tombstones or NullTombstones()
        self.clock = clock

    # -- audit ----------------------------------------------------------------

    def audit(
        self,
        row: SlackInstallation,
        actor: str,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Write one organization audit event; ids and counts only, no secrets."""
        self.audit_events.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=row.org_id,
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type="slack_installation",
                target_id=row.installation_id,
                summary=f"Slack bot {action.rsplit('.', 1)[-1]}",
                request_id=new_id(),
                metadata={
                    "team_id": row.team_id,
                    "status": row.status,
                    **(extra or {}),
                },
                created_at=self.clock(),
            )
        )

    # -- ending ---------------------------------------------------------------

    def end(
        self,
        row: SlackInstallation,
        status: EndStatus,
        *,
        revoke: bool,
        actor: str,
        action: str | None = None,
    ) -> SlackInstallation | None:
        """End a held installation; ``None`` when it was already out of use.

        Run under the installation's organization scope. Repeated events are
        harmless: an ended row is left alone (:meth:`sweep` finishes a token a
        crash left behind).
        """
        if row.status not in HELD_STATUSES:
            return None
        self.tombstones.record(
            SLACK_INSTALLATION_ENDED,
            row.org_id,
            subject_kind=status,
            subject_id=generation(row),
        )
        now = self.clock()
        updated = self.store.set_status(row.installation_id, status, now)
        if updated is None:
            return None
        dropped = self.store.drop_pending_for_team(row.team_id, DROP_REASON_ENDED, now)
        gone = self.retire_token(row, revoke=revoke)
        final = updated
        if gone:  # otherwise the reference stays and the sweep finishes it
            final = (
                self.store.set_status(
                    row.installation_id,
                    status,
                    updated.revoked_at or now,
                    clear_secret=True,
                )
                or updated
            )
        self.audit(
            final,
            actor,
            action or f"slack.installation.{status}",
            {"pending_events_dropped": dropped, "slack_revoke_requested": revoke},
        )
        return final

    def retire_token(
        self, row: SlackInstallation, *, revoke: bool, strict: bool = False
    ) -> bool:
        """Revoke (best effort) and delete the bot token; ``True`` when gone.

        ``strict`` re-raises a failed delete (organization purge must not go on
        without the secret tombstone); otherwise the failure is logged and the
        sweep retries.
        """
        ref = row.bot_secret_ref
        if ref is None:
            return True
        if revoke:
            self.revoke_best_effort(row)
        try:
            self.secrets.delete(ref)
        except Exception as error:
            if strict:
                raise
            logger.error(
                "slack_secret_delete_failed team=%s kind=%s",
                row.team_id,
                type(error).__name__,
            )
            return False
        return True

    def revoke_best_effort(self, row: SlackInstallation) -> None:
        """``auth.revoke`` the stored token; a dead token or Slack error is fine."""
        if row.bot_secret_ref is None:
            return
        try:
            self.api.auth_revoke(self.secrets.get(row.bot_secret_ref))
        except Exception as error:
            logger.warning(
                "slack_revoke_failed team=%s kind=%s",
                row.team_id,
                type(error).__name__,
            )

    def sweep(self, limit: int = 50) -> int:
        """Finish ended installations whose token delete did not complete."""
        done = 0
        for row in self.store.list_unfinished(limit):
            token = tenant_of(row)
            try:
                self.revoke_best_effort(row)
                if self.retire_token(row, revoke=False):
                    self.store.set_status(
                        row.installation_id,
                        row.status,
                        row.revoked_at or self.clock(),
                        clear_secret=True,
                    )
                    done += 1
            except Exception as error:
                logger.error("slack_sweep_failed kind=%s", type(error).__name__)
            finally:
                reset_request_principal(token)
        return done

    # -- quarantine (reversible) ------------------------------------------------

    def suspend_installations(self, ids: list[str], actor: str) -> list[str]:
        """Mark the listed installations ``suspended`` if they are still active."""
        marked = []
        for row in self.store.list_for_org():
            if row.installation_id in ids and row.status == "active":
                self.store.set_status(row.installation_id, "suspended", self.clock())
                self.audit(row, actor, "slack.installation.suspended")
                marked.append(row.installation_id)
        return marked

    def restore_installations(self, installation_ids: list[str], actor: str) -> int:
        """Reactivate only what quarantine suspended and is still suspended."""
        restored = 0
        wanted = set(installation_ids)
        for row in self.store.list_for_org():
            if row.installation_id in wanted and row.status == "suspended":
                self.store.set_status(row.installation_id, "active", self.clock())
                self.audit(row, actor, "slack.installation.resumed")
                restored += 1
        return restored

    def end_installations(self, ids: list[str], actor: str) -> int:
        """End (and revoke at Slack) the listed installations; irreversible."""
        ended = 0
        for row in self.store.list_for_org():
            if row.installation_id in ids and self.end(
                row, "revoked", revoke=True, actor=actor
            ):
                ended += 1
        return ended
