"""Stop and Try again as one entry point for the button and the text fallback.

``RunControls.press`` resolves the run, hands it to the Stop or Try again flow,
writes the one ``slack.interaction`` audit record of the press (action, source,
allowed or blocked, reason code; no text) and returns the private reply the
presser should see, if any. A press that finds no run (a forged reference, a
button from another thread, a run that is gone) is audited as blocked and told
"That's no longer available." without saying which of those it was.
"""

from __future__ import annotations

import logging

from application.ports.slack import SlackInstallation
from modules.slack import copy
from modules.slack.access.service import SlackAccessService
from modules.slack.interactions.audit import InteractionAudit
from modules.slack.interactions.press import Outcome, PressAuth, PressContext
from modules.slack.interactions.retry import RetryFlow
from modules.slack.interactions.stop import StopFlow
from shared.metrics import SLACK_INTERACTIONS

logger = logging.getLogger("product.slack")
_NO_RUN = {
    ("stop", "text"): copy.NO_ACTIVITY,
    ("retry", "text"): copy.NOTHING_TO_RETRY,
}


class RunControls:
    """Authorize, carry out and audit one Stop or Try again."""

    def __init__(
        self,
        *,
        auth: PressAuth,
        stop: StopFlow,
        retry: RetryFlow,
        audit: InteractionAudit,
        access: SlackAccessService,
    ) -> None:
        """Bind the two flows, the press checks and the audit writer."""
        self._auth = auth
        self._stop = stop
        self._retry = retry
        self._audit = audit
        self._access = access

    def press(
        self, installation: SlackInstallation, action: str, ctx: PressContext
    ) -> str | None:
        """Handle one press; returns the private reply for the presser, if any."""
        row = self._auth.resolve(installation, ctx)
        if row is None:
            reply = _NO_RUN.get((action, ctx.source), copy.NO_LONGER_AVAILABLE)
            self._finish(installation, action, ctx, "", Outcome(False, "no_run", reply))
            return reply
        flow = self._stop if action == "stop" else self._retry
        outcome = flow.run(installation, row, ctx)
        self._finish(installation, action, ctx, row.run_ref, outcome)
        return outcome.reply

    def _finish(
        self,
        installation: SlackInstallation,
        action: str,
        ctx: PressContext,
        run_ref: str,
        outcome: Outcome,
    ) -> None:
        SLACK_INTERACTIONS.inc(
            action=action,
            outcome="allowed" if outcome.allowed else "blocked",
            reason=outcome.reason[:40],
        )
        principal, _ = self._access.presser_standing(ctx.user_id)
        self._audit.record(
            org_id=installation.org_id,
            action=action,
            source=ctx.source,
            allowed=outcome.allowed,
            reason=outcome.reason,
            slack_user_id=ctx.user_id,
            principal_id=principal,
            channel_id=ctx.channel_id,
            run_ref=run_ref,
            request_id=ctx.request_id,
            now=ctx.now,
        )
        logger.info(
            "slack_interaction action=%s source=%s outcome=%s reason=%s request=%s",
            action,
            ctx.source,
            "allowed" if outcome.allowed else "blocked",
            outcome.reason,
            ctx.request_id,
        )
