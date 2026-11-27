"""The ``slack`` step of the organization purge (design 3.10).

Runs under the organization's own scope, after credentials are revoked and
before the generic secret purge, so the bot token still exists when it is
revoked at Slack. Per installation the order is fail closed:

1. the 6.2 tombstone for the installation generation (an unwritable log stops
   the step, so nothing is destroyed without a durable record);
2. ``auth.revoke`` at Slack, best effort (a dead token or an outage is fine);
3. the secret delete, whose store decorator writes the secret tombstone first
   and which is allowed to fail the pass (it is retried next tick);
4. only then the installation row.

Inbox rows of the organization are removed in bounded batches. Every sub-step
is idempotent and the row goes last, so a crash anywhere resumes cleanly.
The access settings and allowlists are product-record collections handled by
``PURGE_ORDER``; thread and feedback collections (S4) are removed by the
generic sweep, which deletes any collection the organization still owns.
"""

from __future__ import annotations

from collections.abc import Callable

from application.ports.slack import SlackStore
from modules.recovery.tombstones import SLACK_INSTALLATION_ENDED
from modules.slack.lifecycle import SlackLifecycle, generation
from shared.request_context import effective_org_id


class SlackOrganizationPurge:
    """Delete one organization's installations, tokens and inbox rows."""

    def __init__(
        self,
        lifecycle: SlackLifecycle,
        hook: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the lifecycle (store, secrets, Slack client, tombstones)."""
        self._lifecycle = lifecycle
        self._store: SlackStore = lifecycle.store
        self.hook = hook

    def _step(self, name: str) -> None:
        """Failure-injection seam for crash tests."""
        if self.hook is not None:
            self.hook(name)

    def purge_organization(self, limit: int) -> int:
        """One bounded pass for the bound organization; returns rows removed."""
        removed = 0
        for row in self._store.list_for_org()[:limit]:
            self._step("slack_tombstone")
            self._lifecycle.tombstones.record(
                SLACK_INSTALLATION_ENDED,
                row.org_id,
                subject_kind="purged",
                subject_id=generation(row),
            )
            self._step("slack_revoke")
            self._lifecycle.retire_token(row, revoke=True, strict=True)
            self._step("slack_row")
            if self._store.delete_installation(row.installation_id):
                removed += 1
        self._step("slack_inbox")
        return removed + self._store.delete_org_events(
            effective_org_id(), max(limit - removed, 1)
        )

    def remaining_rows(self) -> int:
        """Slack rows the bound organization still owns."""
        return self._store.count_for_org(effective_org_id())
