"""Try again (design 3.12): the stored request runs again through every gate.

Only a failed request whose failure was delivered can be retried, at most three
times and not after 24 hours. The question is the hidden task's own user message
of the failed attempt (the inbox row's text was cleared long ago). The request
goes through the same gates as a new mention (platform and organization state,
the installation, the bot's pause, the Ask limits with their counting, the tier
rule), is queued as a **new turn** with the idempotency key
``slack:{team}:retry:{run_ref}:{n}`` and counts against the Ask limits like any
other Ask (the worker guard writes its decision at pickup). A previous turn that
is still running (a request that timed out) is cancelled first. The failure
message becomes the working message ("Retrying...") and gets a new Stop button
with a new reference.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from application.ports.repository import Repository
from application.ports.slack import SlackInstallation
from contracts.v1 import TaskMessage, TaskTurn, TurnOrigin
from contracts.v1.slack_threads import SlackThread
from modules.limits.errors import LimitExceeded
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.access.models import Action
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.dispatch.qa_agent import NoModel, QaProvisioner, QaUnavailable
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.interactions.press import (
    Outcome,
    PressAuth,
    PressContext,
    RowLock,
)
from modules.slack.reply.blocks import MAX_RETRIES, stop_button
from modules.slack.reply.deliver import NATIVE, PRIVATE
from modules.slack.reply.loss import InstallationLoss
from modules.slack.reply.poster import PostError, SlackPoster
from modules.slack.reply.status_text import text_for
from modules.tasks.service import TaskService
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack")
RETRY_WINDOW = timedelta(hours=24)


def retry_key(team_id: str, run_ref: str, number: int) -> str:
    """The idempotency key of the ``number``-th retry of a request."""
    return f"slack:{team_id}:retry:{run_ref}:{number}"


class RetryFlow:
    """Carry out one authorized Try again."""

    def __init__(
        self,
        *,
        auth: PressAuth,
        rows: RowLock,
        book: ThreadBook,
        gates: DispatchGates,
        qa: QaProvisioner,
        tasks: TaskService,
        turns: Repository[TaskTurn],
        task_messages: Repository[TaskMessage],
        poster: SlackPoster,
        loss: InstallationLoss,
    ) -> None:
        """Bind the checks, the gates, the task layer and the message edit."""
        self._auth = auth
        self._rows = rows
        self._book = book
        self._gates = gates
        self._qa = qa
        self._tasks = tasks
        self._turns = turns
        self._messages = task_messages
        self._poster = poster
        self._loss = loss

    def run(
        self, installation: SlackInstallation, row: SlackThread, ctx: PressContext
    ) -> Outcome:
        """Retry the failed request of ``row`` for an already resolved press."""
        verdict = self._auth.authorize(row, ctx)
        if not verdict.allowed:
            return Outcome(False, verdict.reason, copy.ONLY_REQUESTER_RETRY)
        refusal = self._refusal(row, ctx)
        if refusal is not None:
            return refusal
        question = self._question(row)
        decision = self._gates.evaluate(
            installation,
            slack_user_id=row.requester_slack_user_id,
            channel_id=row.channel_id,
            action=Action.ASK,
            now=ctx.now,
            question_chars=len(question),
        )
        if not decision.allowed:
            text = decision.message or messages.UNAVAILABLE
            reason = decision.reason.value if decision.reason else "blocked"
            return Outcome(False, reason, copy.refusal(text, decision.outcome.value))
        return self._queue(installation, row, question, ctx, verdict.reason)

    @staticmethod
    def _refusal(row: SlackThread, ctx: PressContext) -> Outcome | None:
        gone = (
            copy.NOTHING_TO_RETRY if ctx.source == "text" else copy.NO_LONGER_AVAILABLE
        )
        failed = (
            row.run_state == "failed"
            and row.reply_state == "posted"
            and row.answer_ts not in (None, PRIVATE)
        )
        if not failed:
            return Outcome(False, "not_failed", gone)
        since = row.finished_at or row.updated_at
        if ctx.now - since > RETRY_WINDOW:
            return Outcome(False, "expired", gone)
        if row.retries >= MAX_RETRIES:
            return Outcome(False, "retry_limit", copy.RETRY_LIMIT)
        return None

    def _question(self, row: SlackThread) -> str:
        turn = self._turns.get_scoped(row.turn_id, row.org_id, row.workspace_id)
        message = self._messages.get_scoped(
            turn.user_message_id, row.org_id, row.workspace_id
        )
        return message.content

    def _queue(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        question: str,
        ctx: PressContext,
        reason: str,
    ) -> Outcome:
        try:
            target = self._qa.ensure(installation.org_id, installation.workspace_id)
        except NoModel:
            return Outcome(False, "no_model", copy.NO_MODEL)
        except QaUnavailable:
            return Outcome(False, "qa_unavailable", messages.UNAVAILABLE)
        number = row.retries + 1
        token = set_request_identity(
            target.principal_id, installation.org_id, installation.workspace_id
        )
        try:
            # A request that timed out may still be running: end it first.
            self._tasks.cancel_origin_turn(row.task_id, row.turn_id)
            turn = self._tasks.add_message(
                row.task_id,
                question,
                idempotency_key=retry_key(row.team_id, row.run_ref, number),
                origin=self._origin(row, number),
            )
        except LimitExceeded as error:
            wait = error.retry_after_seconds
            text = messages.rate_limited(wait) if wait else messages.UNAVAILABLE
            return Outcome(False, "limit", copy.refusal(text, "rate_limited"))
        except ValueError:
            return Outcome(True, "already_retrying", copy.ALREADY_RETRYING)
        finally:
            reset_request_principal(token)
        return self._renew(installation, row, turn.turn_id, ctx, reason)

    def _origin(self, row: SlackThread, number: int) -> TurnOrigin:
        return TurnOrigin(
            kind="slack_qa",
            team_id=row.team_id,
            channel_id=row.channel_id,
            thread_ts=row.thread_ts,
            slack_user_id=row.requester_slack_user_id,
            event_id=f"retry.{number}.{row.event_id}"[:64],
            settings_version=self._gates.settings_version(),
            action="ask",
        )

    def _renew(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        turn_id: str,
        ctx: PressContext,
        reason: str,
    ) -> Outcome:
        """Point the row at the new turn (compare-and-set) and show "Retrying…"."""
        shown = _editable(row)
        renewed = self._rows.change(
            row,
            lambda fresh: fresh.run_state == "failed" and fresh.retries == row.retries,
            lambda fresh: self._book.retried(
                fresh, turn_id=turn_id, status_ts=shown, now=ctx.now
            ),
            ctx.now,
        )
        if renewed is None:
            return Outcome(True, "already_retrying", copy.ALREADY_RETRYING)
        if shown is not None:
            self._show_retrying(installation, renewed, shown)
        return Outcome(True, reason)

    def _show_retrying(
        self, installation: SlackInstallation, row: SlackThread, ts: str
    ) -> None:
        try:
            self._poster.edit(
                installation,
                row.channel_id,
                ts,
                text_for("retrying"),
                button=stop_button(row.run_ref),
            )
        except PostError as error:
            logger.warning("slack_retry_edit_failed code=%s", error.code)
            if error.kind == "token":
                self._loss(installation)


def _editable(row: SlackThread) -> str | None:
    """The failure message of the row, if it is a message we can edit."""
    for ts in (row.answer_ts, row.status_ts):
        if ts and ts not in (NATIVE, PRIVATE):
            return ts
    return None
