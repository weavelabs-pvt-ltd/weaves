"""The ``slack_interactions`` worker tick: carry out the presses the endpoint stored.

The endpoint acks within Slack's three seconds and only inserts an inbox row of
type ``interaction`` (its dedupe record). This tick reads the pending rows oldest
first, resolves the team to its installation (an ended workspace drops the press),
claims the row with a compare-and-set (two workers never carry out one press) and
runs Stop or Try again under the organization's tenant context. The reply, if the
presser needs one, is a private message in the thread; it is never posted
publicly. Unlike the dispatcher this tick is **not** held by the operator worker
pause, the bot's own pause or quarantine, because Stop must keep working then.
A press that fails after it was claimed is logged by kind and not retried: the
button is still there, so the person can press it again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

from application.ports.slack import InboxEvent, SlackInstallation, SlackStore
from modules.slack.events import tenant_of
from modules.slack.interactions.controls import RunControls
from modules.slack.interactions.press import PressContext
from modules.slack.reply.loss import InstallationLoss
from modules.slack.reply.poster import PostError, SlackPoster
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.metrics import SLACK_INTERACTIONS

logger = logging.getLogger("product.slack")
BATCH = 25
TICK_BUDGET_SECONDS = 15.0
INTERACTION = "interaction"


class SlackInteractionTick:
    """Handle pending interaction inbox rows."""

    def __init__(
        self,
        *,
        store: SlackStore,
        controls: RunControls,
        poster: SlackPoster,
        loss: InstallationLoss,
        enabled: Callable[[], bool],
        clock: Callable[[], datetime] = utc_now,
        budget_seconds: float = TICK_BUDGET_SECONDS,
    ) -> None:
        """Bind the store, the Stop and Try again flows and the private reply."""
        self._store = store
        self._controls = controls
        self._poster = poster
        self._loss = loss
        self._enabled = enabled
        self._clock = clock
        self._budget = budget_seconds

    def run(self) -> int:
        """Carry out a batch of pending presses; returns how many were claimed."""
        started = time.monotonic()
        handled = 0
        for event in self._store.list_events(
            state="pending", limit=BATCH, event_type=INTERACTION
        ):
            if time.monotonic() - started > self._budget:
                break
            try:
                handled += self._handle(event)
            except Exception as error:
                SLACK_INTERACTIONS.inc(action="unknown", outcome="error", reason="")
                logger.error(
                    "slack_interaction_failed request=%s kind=%s",
                    event.request_id,
                    type(error).__name__,
                )
        return handled

    def _handle(self, event: InboxEvent) -> int:
        installation = self._store.find_by_team(event.team_id)
        if installation is None or not self._enabled():
            reason = "installation_inactive" if installation is None else "disabled"
            return int(
                self._store.settle_event(event.event_id, "dropped", reason=reason)
            )
        if not self._store.settle_event(event.event_id, "done"):
            return 0  # another worker has it
        action = (event.reaction or "").removeprefix("weaves.")
        if action not in ("stop", "retry") or not event.text:
            return 1
        ctx = PressContext(
            channel_id=event.channel_id or "",
            thread_ts=event.thread_ts,
            user_id=event.user_id or "",
            source="button",
            request_id=event.request_id or f"slack-{event.event_id[:48]}",
            now=self._clock(),
            ref=event.text,
            message_ts=event.message_ts,
        )
        with tenant_of(installation), extend_context(request_id=ctx.request_id):
            reply = self._controls.press(installation, action, ctx)
            if reply:
                self._whisper(installation, ctx, reply)
        return 1

    def _whisper(
        self, installation: SlackInstallation, ctx: PressContext, text: str
    ) -> None:
        """Tell the presser privately (never in the channel)."""
        try:
            self._poster.ephemeral(
                installation,
                ctx.channel_id,
                ctx.user_id,
                text,
                thread_ts=ctx.thread_ts,
            )
        except PostError as error:
            logger.warning("slack_interaction_reply_failed code=%s", error.code)
            if error.kind == "token":
                self._loss(installation)
