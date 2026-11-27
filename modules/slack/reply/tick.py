"""The ``slack_reply`` worker tick: keep each Slack request's messages up to date.

Every pending thread row (a request whose reply is not yet delivered) is looked
at once per tick, per installation, under that organization's tenant context:

* the turn is queued or running: show or refresh the working status (a queued
  notice after 10 s, the real step, "still working", "taking longer"); past the
  deadline (15 minutes) the request fails with a short reference;
* the turn finished: build the reply from the verdict and the run (answer,
  refusal, failure), deliver it exactly once, then turn the working message into
  the receipt and close the row.

A post that fails is retried with backoff and then given up on
(``undeliverable``). Nothing here is allowed to disturb the worker loop: each row
is isolated and every Slack call has its own timeout. Message text is never logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta

from application.ports.repository import Repository
from application.ports.slack import SlackInstallation, SlackStore
from contracts.v1 import AgentRun, TaskMessage, TaskTurn, TaskTurnStatus
from contracts.v1.slack_threads import SlackThread
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.events import tenant_of
from modules.slack.reply import compose
from modules.slack.reply.deliver import Delivery, Target
from modules.slack.reply.kinds import ReplyKind
from modules.slack.reply.loss import InstallationLoss
from modules.slack.reply.poster import PostError
from modules.slack.reply.status_text import DEFAULT_DEADLINE, desired_key
from modules.slack.reply.steps import RunSteps
from modules.slack.reply.throttle import NoticeThrottle
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.metrics import SLACK_REPLY

logger = logging.getLogger("product.slack")
LEASE = timedelta(seconds=45)
MAX_ATTEMPTS = 6
MAX_BACKOFF = 300.0
ROWS_PER_INSTALLATION = 25
MAX_ROW_FAILURES = 10
MAX_TRACKED_FAILURES = 1000
TICK_BUDGET_SECONDS = 20.0


def backoff_seconds(attempts: int) -> float:
    """5, 10, 20, 40, 80, 160 seconds, never more than five minutes."""
    return float(min(MAX_BACKOFF, 5 * 2 ** max(0, attempts - 1)))


class SlackReplyTick:
    """The worker tick that posts and edits Slack messages for pending requests."""

    def __init__(
        self,
        *,
        store: SlackStore,
        threads: Repository[SlackThread],
        turns: Repository[TaskTurn],
        task_messages: Repository[TaskMessage],
        agent_runs: Repository[AgentRun],
        book: ThreadBook,
        steps: RunSteps,
        delivery: Delivery,
        loss: InstallationLoss,
        enabled: Callable[[], bool],
        org_active: Callable[[str], bool],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        lock: Callable[[str], None] | None,
        native_status: Callable[[], bool] = lambda: False,
        kinds: Mapping[str, ReplyKind] | None = None,
        deadline: timedelta = DEFAULT_DEADLINE,
        clock: Callable[[], datetime] = utc_now,
        budget_seconds: float = TICK_BUDGET_SECONDS,
    ) -> None:
        """Bind the records, the delivery steps and the state readers."""
        self._store = store
        self._threads = threads
        self._turns = turns
        self._messages = task_messages
        self._agent_runs = agent_runs
        self._book = book
        self._steps = steps
        self._delivery = delivery
        self._loss = loss
        self._enabled = enabled
        self._org_active = org_active
        self._uow = unit_of_work
        self._lock = lock
        self._native = native_status
        self._kinds: Mapping[str, ReplyKind] = kinds or {}
        self._deadline = deadline
        self._clock = clock
        self._budget = budget_seconds
        self._throttle = NoticeThrottle()
        self._failures: dict[str, int] = {}

    # -- the tick -------------------------------------------------------------

    def run(self) -> int:
        """Visit every installation's pending requests; returns rows that changed."""
        if not self._enabled():
            return 0
        started = time.monotonic()
        changed = 0
        for held in self._store.list_held():
            with tenant_of(held):
                for row in self._pending(held):
                    if time.monotonic() - started > self._budget:
                        return changed
                    changed += self._visit(held, row)
        return changed

    def _pending(self, held: SlackInstallation) -> list[SlackThread]:
        rows = [
            r
            for r in self._threads.list_by_field("reply_state", "pending")
            if r.org_id == held.org_id and r.team_id == held.team_id
        ]
        rows.sort(key=lambda r: (r.requested_at, r.id))
        return rows[:ROWS_PER_INSTALLATION]

    def _visit(self, held: SlackInstallation, row: SlackThread) -> int:
        now = self._clock()
        if row.next_attempt_at is not None and row.next_attempt_at > now:
            return 0  # backing off, or another worker holds the lease
        try:
            with extend_context(request_id=row.request_id):
                return self._step(held, row, now)
        except Exception as error:
            return self._poisoned(held, row, error, now)

    def _poisoned(
        self, held: SlackInstallation, row: SlackThread, error: Exception, now: datetime
    ) -> int:
        """Count a failing row; one that keeps failing is given up on."""
        count = self._failures.get(row.id, 0) + 1
        if len(self._failures) < MAX_TRACKED_FAILURES:
            self._failures[row.id] = count
        logger.error(
            "slack_reply_row_failed event=%s attempt=%d kind=%s",
            row.event_id,
            count,
            type(error).__name__,
        )
        SLACK_REPLY.inc(kind="row", outcome="error")
        if count < MAX_ROW_FAILURES:
            return 0
        self._failures.pop(row.id, None)
        try:
            return self._give_up(row, Target(held, now), "internal_error")
        except Exception:
            return 0

    # -- one row ---------------------------------------------------------------

    def _step(self, held: SlackInstallation, row: SlackThread, now: datetime) -> int:
        target = Target(held, now, native_status=self._native())
        if held.status != "active" or not self._org_active(held.org_id):
            return self._unavailable(held, row, target)
        try:
            turn = self._turns.get_scoped(row.turn_id, row.org_id, row.workspace_id)
        except KeyError:
            return self._give_up(row, target, "turn_missing")
        action = self._plan(row, turn, now)
        if action is None:
            return 0
        claimed = self._claim(row, now)
        if claimed is None:
            return 0
        return self._execute(held, claimed, turn, action, target)

    def _plan(
        self, row: SlackThread, turn: TaskTurn, now: datetime
    ) -> tuple[str, str] | None:
        """What the row needs now: ``(kind, detail)`` or ``None`` for nothing."""
        if row.answer_ts is not None:
            return ("receipt", "")
        if turn.status in (TaskTurnStatus.QUEUED, TaskTurnStatus.RUNNING):
            return self._plan_working(row, turn, now)
        return ("reply", turn.status.value)

    def _plan_working(
        self, row: SlackThread, turn: TaskTurn, now: datetime
    ) -> tuple[str, str] | None:
        age = now - row.requested_at
        if age >= self._deadline:
            return ("timeout", "")
        running = turn.status is TaskTurnStatus.RUNNING
        verdict = turn.origin.verdict if turn.origin else None
        kind = self._kinds.get(row.kind)
        if kind is not None:
            key = kind.working_key(row, turn, age, running)
        else:
            waiting = self._steps.waiting_for(turn) if running else []
            key = desired_key(
                age=age,
                running=running,
                guard_done=verdict is not None,
                waiting_for=waiting,
            )
        if key is None or key == row.status_text_key:
            return None
        return ("status", key)

    def _claim(self, row: SlackThread, now: datetime) -> SlackThread | None:
        """Take the row for this worker (compare-and-set under an advisory lock)."""
        with self._uow():
            if self._lock is not None:
                self._lock(f"weaves-slack-reply:{row.id}")
            fresh = self._threads.get_scoped(row.id, row.org_id)
            if (
                fresh.updated_at != row.updated_at
                or fresh.reply_state != "pending"
                or (fresh.next_attempt_at is not None and fresh.next_attempt_at > now)
            ):
                return None
            return self._book.save(
                fresh.model_copy(update={"next_attempt_at": now + LEASE}), now
            )

    def _execute(
        self,
        held: SlackInstallation,
        row: SlackThread,
        turn: TaskTurn,
        action: tuple[str, str],
        target: Target,
    ) -> int:
        """Run one planned step; ``row`` always holds the latest saved state."""
        kind, detail = action
        try:
            if kind == "status":
                running = turn.status is TaskTurnStatus.RUNNING
                row = row.model_copy(
                    update={"run_state": "running" if running else "queued"}
                )
                kind_words = self._kinds.get(row.kind)
                words = (
                    kind_words.working_text(row, turn, detail) if kind_words else None
                )
                row = self._delivery.show_status(row, detail, target, words)
                self._release(row, target.now)
            elif kind == "receipt":
                self._delivery.finish_status(row, target, self._receipt(row, turn))
                SLACK_REPLY.inc(kind="receipt", outcome="closed")
            else:
                self._deliver(row, turn, kind, target)
        except PostError as error:
            self._failed(held, self._latest(row), error, target)
        return 1

    def _latest(self, row: SlackThread) -> SlackThread:
        """The stored row (a step may have saved progress before it failed)."""
        try:
            return self._threads.get_scoped(row.id, row.org_id)
        except KeyError:
            return row

    def _deliver(
        self, row: SlackThread, turn: TaskTurn, kind: str, target: Target
    ) -> None:
        reply = (
            self._timeout_reply(turn)
            if kind == "timeout"
            else self._reply(row, turn, target.now)
        )
        if reply is None:  # stopped without a verdict: nothing to say
            self._book.save(
                row.model_copy(
                    update={
                        "run_state": "stopped",
                        "reply_state": "posted",
                        "finished_at": target.now,
                        "next_attempt_at": None,
                    }
                ),
                target.now,
            )
            return
        delivered = self._delivery.send_reply(row, reply, target)
        self._delivery.finish_status(delivered, target, self._receipt(delivered, turn))

    def _receipt(self, row: SlackThread, turn: TaskTurn) -> str | None:
        """The kind's own receipt text (``None``: the default "Done in 41s")."""
        words = self._kinds.get(row.kind)
        return words.receipt_text(row, turn) if words else None

    def _release(self, row: SlackThread, now: datetime) -> None:
        """Drop the lease after a status step so the next change is not delayed."""
        self._book.save(row.model_copy(update={"next_attempt_at": None}), now)

    # -- building the reply ------------------------------------------------------

    @staticmethod
    def _timeout_reply(turn: TaskTurn) -> compose.Reply:
        return compose.Reply("failure", copy.failure(compose.short_ref(turn.turn_id)))

    def _reply(
        self, row: SlackThread, turn: TaskTurn, now: datetime
    ) -> compose.Reply | None:
        words = self._kinds.get(row.kind)
        if words is not None:
            return words.reply(row, turn, now)
        verdict = turn.origin.verdict if turn.origin else None
        if turn.status is TaskTurnStatus.FAILED:
            return compose.failure(turn)
        if turn.status is TaskTurnStatus.CANCELLED:
            if verdict is None:
                return None
            return compose.refusal(verdict.message, verdict.outcome)
        if turn.agent_run_id is None:  # the guard short-circuited the run
            if verdict is None or not verdict.message:
                return compose.refusal(None, "refused_unavailable")
            return compose.refusal(verdict.message, verdict.outcome)
        return self._answer(row, turn, now)

    def _answer(self, row: SlackThread, turn: TaskTurn, now: datetime) -> compose.Reply:
        content = self._messages.get_scoped(
            turn.assistant_message_id or "", turn.org_id, turn.workspace_id
        ).content
        run = self._agent_runs.get_scoped(
            turn.agent_run_id or "", turn.org_id, turn.workspace_id
        )
        summary = run.output_summary or {}
        citations = summary.get("citations", [])
        titles = [str(c.get("title", "")) for c in citations if isinstance(c, Mapping)]
        sources = len(summary.get("source_ids", []) or citations)
        verdict = turn.origin.verdict if turn.origin else None
        elapsed = ((turn.finished_at or now) - row.requested_at).total_seconds()
        text = compose.answer(
            content,
            titles=titles,
            sources=sources,
            elapsed_seconds=elapsed,
            skipped_note=verdict.skipped_note if verdict else None,
        )
        return compose.Reply(
            "answer",
            text,
            ephemeral=bool(verdict and verdict.answer_ephemeral),
            sources=sources,
        )

    # -- failures ------------------------------------------------------------------

    def _failed(
        self,
        held: SlackInstallation,
        row: SlackThread,
        error: PostError,
        target: Target,
    ) -> None:
        if error.kind == "token":
            self._loss(held)
            self._give_up(row, target, error.code)
            return
        attempts = row.reply_attempts + 1
        if error.kind in ("permanent", "scrubber") or attempts >= MAX_ATTEMPTS:
            self._give_up(row, target, error.code)
            return
        wait = max(error.retry_after, backoff_seconds(attempts))
        self._book.save(
            row.model_copy(
                update={
                    "reply_attempts": attempts,
                    "reply_error": error.code[:64],
                    "next_attempt_at": target.now + timedelta(seconds=wait),
                }
            ),
            target.now,
        )
        SLACK_REPLY.inc(kind="post", outcome="retry")

    def _give_up(self, row: SlackThread, target: Target, code: str) -> int:
        delivered = row.answer_ts is not None  # only the receipt edit was lost
        self._book.save(
            row.model_copy(
                update={
                    "reply_state": "posted" if delivered else "undeliverable",
                    "reply_error": code[:64],
                    "next_attempt_at": None,
                    "run_state": "failed"
                    if row.run_state not in ("done", "stopped")
                    else row.run_state,
                    "finished_at": row.finished_at or target.now,
                }
            ),
            target.now,
        )
        SLACK_REPLY.inc(kind="post", outcome="undeliverable")
        logger.warning(
            "slack_reply_undeliverable event=%s request=%s code=%s",
            row.event_id,
            row.request_id,
            code[:64],
        )
        return 1

    def _unavailable(
        self, held: SlackInstallation, row: SlackThread, target: Target
    ) -> int:
        """A suspended installation or quarantined organization posts nothing of ours.

        The asker gets one neutral private line, at most one per channel per hour.
        """
        SLACK_REPLY.inc(kind="blocked", outcome="withheld")
        if held.status == "active" and self._throttle.allow(
            row.team_id, row.channel_id, target.now
        ):
            try:
                self._delivery.notice(row, messages.UNAVAILABLE, target)
            except PostError as error:
                logger.warning("slack_notice_failed code=%s", error.code)
        return self._give_up(row, target, "org_unavailable")
