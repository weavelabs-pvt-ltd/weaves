"""Stop (design 3.12): cancel the turn, discard a late answer, edit to "Stopped.".

1. Under the reply tick's advisory lock the row goes ``queued|running`` to
   ``cancel_requested`` (compare-and-set, so a double press finds it already set).
2. The hidden turn is cancelled through the task layer's own cancellation path:
   the worker's lease is revoked and its cancellation check, polled at stage
   boundaries of the run, ends the run; any answer it still produces is refused
   because it no longer holds the turn.
3. The working message is edited to ``Stopped.`` without buttons and the row is
   closed (``stopped``). If Slack cannot be reached the row stays
   ``cancel_requested`` and the reply tick finishes the edit; it also treats a
   cancelled turn as stopped, so nothing is ever posted for a stopped request.

Tokens already spent still settle in the usage ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime

from application.ports.slack import SlackInstallation
from contracts.v1.slack_threads import SlackThread
from modules.slack import copy
from modules.slack.interactions.press import (
    Outcome,
    PressAuth,
    PressContext,
    RowLock,
)
from modules.slack.reply.deliver import Delivery, Target
from modules.slack.reply.loss import InstallationLoss
from modules.slack.reply.poster import PostError
from modules.tasks.service import TaskService

logger = logging.getLogger("product.slack")
STOP_STATES = ("cancel_requested", "stopped")
RUNNING_STATES = ("queued", "running")


def _running(row: SlackThread) -> bool:
    return (
        row.run_state in RUNNING_STATES
        and row.reply_state == "pending"
        and row.answer_ts is None
    )


class StopFlow:
    """Carry out one authorized Stop."""

    def __init__(
        self,
        *,
        auth: PressAuth,
        rows: RowLock,
        delivery: Delivery,
        tasks: TaskService,
        loss: InstallationLoss,
    ) -> None:
        """Bind the press checks, the row lock, the message edit and the task layer."""
        self._auth = auth
        self._rows = rows
        self._delivery = delivery
        self._tasks = tasks
        self._loss = loss

    def run(
        self, installation: SlackInstallation, row: SlackThread, ctx: PressContext
    ) -> Outcome:
        """Stop the run of ``row`` for an already resolved press."""
        verdict = self._auth.authorize(row, ctx)
        if not verdict.allowed:
            return Outcome(False, verdict.reason, copy.ONLY_REQUESTER_STOP)
        if row.run_state in STOP_STATES:
            return Outcome(True, "already_stopped", copy.ALREADY_STOPPED)
        if not _running(row):
            return Outcome(False, "not_running", self._gone(ctx))
        claimed = self._rows.change(
            row,
            lambda fresh: _running(fresh) and fresh.turn_id == row.turn_id,
            lambda fresh: fresh.model_copy(update={"run_state": "cancel_requested"}),
            ctx.now,
        )
        if claimed is None:
            return self._lost_race(row)
        self._tasks.cancel_origin_turn(claimed.task_id, claimed.turn_id)
        self._finish(installation, claimed, ctx.now)
        return Outcome(True, verdict.reason)

    @staticmethod
    def _gone(ctx: PressContext) -> str:
        return copy.NO_ACTIVITY if ctx.source == "text" else copy.NO_LONGER_AVAILABLE

    def _lost_race(self, row: SlackThread) -> Outcome:
        """Someone else changed the row first: stopped by them, or finished."""
        stored = self._rows.read(row)
        if stored is not None and stored.run_state in STOP_STATES:
            return Outcome(True, "already_stopped", copy.ALREADY_STOPPED)
        return Outcome(False, "not_running", copy.NO_LONGER_AVAILABLE)

    def _finish(
        self, installation: SlackInstallation, row: SlackThread, now: datetime
    ) -> None:
        """Edit the message and close the row; the tick retries a failed edit."""
        try:
            self._delivery.stopped(row, Target(installation, now))
        except PostError as error:
            logger.warning("slack_stop_edit_failed code=%s", error.code)
            if error.kind == "token":
                self._loss(installation)
