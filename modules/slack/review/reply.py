"""What a review says in the thread: the working message, the receipt, the review.

Message one names the pull request and its file count while the review runs and
is edited into the receipt; message two is the review itself, posted as a new
message so the asker is notified. Failures replace the working message with a
fixed sentence: the PR cannot be read, the model's answer broke the output
contract, GitHub is limiting requests, or the generic reference line. The model's
text is validated strictly (``parse_review``) and every untrusted string is
sanitized by ``render`` before it is placed in a template.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from application.ports.repository import Repository
from contracts.v1 import TaskMessage, TaskTurn, TaskTurnStatus
from contracts.v1.slack_threads import SlackThread
from modules.slack.reply import compose
from modules.slack.reply.compose import Reply
from modules.slack.reply.status_text import (
    QUEUED_AFTER,
    STILL_WORKING_AFTER,
    TAKING_LONGER_AFTER,
    text_for,
)
from modules.slack.review import render
from modules.slack.review.result import parse_review
from modules.slack.review.stats import ReviewProgress
from shared.metrics import SLACK_REVIEW

START = "rv:start"
READY = "rv:ready"
INVALID_OUTPUT = "agent.output_invalid"


class ReviewReply:
    """The ``review`` kind of the reply tick."""

    def __init__(
        self, *, progress: ReviewProgress, task_messages: Repository[TaskMessage]
    ) -> None:
        """Bind the reader's progress records and the task's messages."""
        self._progress = progress
        self._messages = task_messages

    # -- while it runs ----------------------------------------------------------

    def working_key(
        self, row: SlackThread, turn: TaskTurn, age: timedelta, running: bool
    ) -> str | None:
        """Queued after 10 s, the pull request once read, then the long waits."""
        if not running:
            return "queued" if age >= QUEUED_AFTER else None
        if age >= TAKING_LONGER_AFTER:
            return "longer"
        if age >= STILL_WORKING_AFTER:
            return f"still:{int(age.total_seconds() // 60)}"
        return READY if self._progress.stats(turn) is not None else START

    def working_text(self, row: SlackThread, turn: TaskTurn, key: str) -> str | None:
        """The first message: the pull request (and its title and files once known)."""
        if key == READY:
            stats = self._progress.stats(turn)
            if stats is not None:
                return render.working(stats)
        if key in (START, READY):
            return render.working_start(_ref(turn))
        return text_for(key)

    # -- when it is done ----------------------------------------------------------

    def reply(self, row: SlackThread, turn: TaskTurn, now: datetime) -> Reply | None:
        """The review, or the fixed sentence that says why there is none."""
        verdict = turn.origin.verdict if turn.origin else None
        if turn.status is TaskTurnStatus.FAILED:
            return self._failed(turn)
        if turn.status is TaskTurnStatus.CANCELLED:
            if verdict is None:
                return None
            return _refused(verdict.message, verdict.outcome)
        if turn.agent_run_id is None:  # the guard short-circuited the run
            if verdict is None or not verdict.message:
                return _refused(None, "refused_unavailable")
            return _refused(verdict.message, verdict.outcome)
        return self._review(row, turn)

    def _failed(self, turn: TaskTurn) -> Reply:
        code = self._progress.error_code(turn)
        if code in render.UNREADABLE_CODES:
            return _said("unreadable", render.UNREADABLE)
        if code == "plugin.pr_rate_limited":
            return _said("rate_limited", render.GITHUB_BUSY)
        if turn.error is not None and turn.error.code == INVALID_OUTPUT:
            return _said("invalid_output", render.NO_REVIEW)
        SLACK_REVIEW.inc(outcome="failed")
        return compose.failure(turn)

    def _review(self, row: SlackThread, turn: TaskTurn) -> Reply:
        try:
            content = self._messages.get_scoped(
                turn.assistant_message_id or "", turn.org_id, turn.workspace_id
            ).content
        except KeyError:
            return _said("invalid_output", render.NO_REVIEW)
        review = parse_review(content)
        stats = self._progress.stats(turn)
        if review is None or stats is None:
            return _said("invalid_output", render.NO_REVIEW)
        partial = stats.files_reviewed < stats.files_total
        SLACK_REVIEW.inc(outcome="partial" if partial else "reviewed")
        return Reply("answer", render.review_message(review, stats, row.task_id))

    def receipt_text(self, row: SlackThread, turn: TaskTurn) -> str | None:
        """``Reviewed acme/api#123, "title" · 14 of 14 files · 38s``."""
        stats = self._progress.stats(turn)
        if stats is None:
            return None
        end = row.finished_at or turn.finished_at
        if end is None:
            return None
        return render.receipt(stats, (end - row.requested_at).total_seconds())


def _ref(turn: TaskTurn) -> str:
    origin = turn.origin
    if origin is None or not origin.repo or origin.pr_number is None:
        return "the pull request"
    return f"{origin.repo}#{origin.pr_number}"


def _said(outcome: str, text: str) -> Reply:
    """A fixed failure sentence (and its metric)."""
    SLACK_REVIEW.inc(outcome=outcome)
    return Reply("failure", text)


def _refused(message: str | None, outcome: str) -> Reply:
    SLACK_REVIEW.inc(outcome="refused")
    return compose.refusal(message, outcome)
