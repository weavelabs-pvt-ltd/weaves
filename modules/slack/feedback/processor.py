"""The ``slack_feedback`` worker tick: ``reaction_added`` inbox rows to ratings.

Design 3.13. The events endpoint stores only thumbs up and down (any skin tone)
and never the bot's own reactions. This tick settles each stored row exactly
once. A reaction counts only when

* the installation, organization and bot are in a state where the bot may speak
  (the S3 state gate: platform, organization, installation, bot pause),
* the reacted message is one of our answers (channel and ``ts`` match the
  ``answer_ts`` of a thread row; the working message, the asker's own message and
  anyone else's message are not answers), and
* the reactor is the person who asked (the Slack user of the thread's request).

The latest reaction by time wins: the row of one turn and asker is replaced when
a newer reaction differs, and an older delivery never overwrites a newer one. The
platform ``read_only`` flag holds rows (they stay pending, retention removes
them after 14 days); every other refusal drops the row with a reason code.
Nothing here reads, stores or logs message text, a reason or a raw Slack user id.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from application.ports.repository import Repository
from application.ports.slack import InboxEvent, SlackInstallation, SlackStore
from contracts.v1 import AgentRun, TaskTurn
from contracts.v1.slack_feedback import SlackFeedback, feedback_record_id
from contracts.v1.slack_threads import SlackThread
from modules.slack.access.models import Decision
from modules.slack.dispatch.ask_flow import request_id_of
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.events import tenant_of
from modules.slack.feedback.asker_ref import AskerRef, AskerRefs
from modules.slack.feedback.reaction import feedback_value
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.metrics import SLACK_FEEDBACK, SLACK_INBOX_AGE

logger = logging.getLogger("product.slack")
BATCH = 25
TICK_BUDGET_SECONDS = 15.0
MAX_FAILURES = 5
MAX_TRACKED_FAILURES = 1000
_NO_ANSWER = (None, "", "private")


class SlackFeedbackTick:
    """Turn pending thumbs reactions into feedback rows."""

    def __init__(
        self,
        *,
        store: SlackStore,
        gates: DispatchGates,
        threads: Repository[SlackThread],
        turns: Repository[TaskTurn],
        agent_runs: Repository[AgentRun],
        feedback: Repository[SlackFeedback],
        refs: AskerRefs,
        enabled: Callable[[], bool],
        read_only: Callable[[], bool],
        clock: Callable[[], datetime] = utc_now,
        budget_seconds: float = TICK_BUDGET_SECONDS,
    ) -> None:
        """Bind the inbox, the state gate and the collections it reads and writes."""
        self._store = store
        self._gates = gates
        self._threads = threads
        self._turns = turns
        self._runs = agent_runs
        self._feedback = feedback
        self._refs = refs
        self._enabled = enabled
        self._read_only = read_only
        self._clock = clock
        self._budget = budget_seconds
        self._failures: dict[str, int] = {}

    def run(self) -> int:
        """Settle a batch of pending reactions; returns how many were settled."""
        started = time.monotonic()
        handled = 0
        for event in self._store.list_events(
            state="pending", limit=BATCH, event_type="reaction_added"
        ):
            if time.monotonic() - started > self._budget:
                break
            try:
                handled += self._handle(event)
                self._failures.pop(event.event_id, None)
            except Exception as error:
                handled += self._failed(event, error)
        return handled

    def _failed(self, event: InboxEvent, error: Exception) -> int:
        """A row that keeps failing is dropped so it cannot starve the rows behind."""
        count = self._failures.get(event.event_id, 0) + 1
        if len(self._failures) < MAX_TRACKED_FAILURES:
            self._failures[event.event_id] = count
        logger.error(
            "slack_feedback_failed event=%s attempt=%d kind=%s",
            event.event_id,
            count,
            type(error).__name__,
        )
        if count < MAX_FAILURES:
            return 0
        self._failures.pop(event.event_id, None)
        return self._drop(event, "feedback_error", None)

    # -- one event -------------------------------------------------------------

    def _handle(self, event: InboxEvent) -> int:
        now = self._clock()
        SLACK_INBOX_AGE.observe(max(0.0, (now - event.received_at).total_seconds()))
        value = feedback_value(event.reaction)
        installation = self._store.find_by_team(event.team_id)
        if installation is None or installation.status != "active":
            return self._drop(event, "installation_inactive", value)
        if not self._enabled():
            return self._drop(event, "slack_disabled", value)
        if self._read_only():
            return 0  # held: the row stays pending until the platform is writable
        with tenant_of(installation), extend_context(request_id=request_id_of(event)):
            blocked = self._gates.state(
                installation,
                slack_user_id=event.user_id or "",
                channel_id=event.channel_id or "",
                now=now,
            )
            if blocked is not None:
                return self._drop(event, _reason(blocked), value)
            return self._rate(installation, event, value)

    def _rate(
        self, installation: SlackInstallation, event: InboxEvent, value: int | None
    ) -> int:
        user = event.user_id or ""
        if value is None:
            return self._drop(event, "not_feedback", value)
        if user == installation.bot_user_id:
            return self._drop(event, "own_reaction", value)
        thread = self._answered_thread(installation, event)
        if thread is None:
            return self._drop(event, "not_our_answer", value)
        if thread.requester_slack_user_id != user:
            return self._drop(event, "not_the_asker", value)
        outcome = self._record(thread, event, value, self._refs.resolve(user))
        return self._settled(event, outcome, value)

    def _answered_thread(
        self, installation: SlackInstallation, event: InboxEvent
    ) -> SlackThread | None:
        ts = event.message_ts
        if ts in _NO_ANSWER:
            return None
        for row in self._threads.list_by_field("answer_ts", ts or ""):
            if (
                row.org_id == installation.org_id
                and row.team_id == installation.team_id
                and row.channel_id == event.channel_id
            ):
                return row
        return None

    # -- writing ---------------------------------------------------------------

    def _record(
        self, thread: SlackThread, event: InboxEvent, value: int, asker: AskerRef
    ) -> str:
        """Upsert the asker's rating of the turn; latest by time wins."""
        at = event.received_at
        rows = self._feedback.list_by_field("turn_id", thread.turn_id)
        mine = [r for r in rows if r.asker_ref in (asker.ref, asker.hashed)]
        if any(r.at > at for r in mine):
            return "stale"
        previous = next((r for r in mine if r.asker_ref == asker.ref), None)
        if previous and (previous.value, previous.at) == (value, at) and len(mine) == 1:
            return "repeated"  # a replay of the event that wrote this row
        now = self._clock()
        row = SlackFeedback(
            id=feedback_record_id(thread.org_id, thread.turn_id, asker.ref),
            org_id=thread.org_id,
            answer_ref=f"{thread.channel_id}:{event.message_ts}",
            turn_id=thread.turn_id,
            task_id=thread.task_id,
            asker_ref=asker.ref,
            asker_kind="member" if asker.kind == "member" else "hashed",
            value=1 if value == 1 else -1,
            at=at,
            created_at=previous.created_at if previous else now,
            updated_at=now,
            request_id=thread.request_id,
            **self._snapshot(thread),
        )
        self._feedback.put(row)
        for other in mine:  # an asker mapped (or unmapped) since: one row per turn
            if other.id != row.id:
                self._feedback.delete(other.id)
        if previous is None:
            return "recorded"
        return "changed" if previous.value != row.value else "repeated"

    def _snapshot(self, thread: SlackThread) -> dict[str, Any]:
        """Run facts kept with the rating, so reports outlive the run records."""
        data: dict[str, Any] = {}
        try:
            turn = self._turns.get_scoped(
                thread.turn_id, thread.org_id, thread.workspace_id
            )
        except KeyError:
            return data
        if turn.origin is not None and turn.origin.capability_ids:
            ids = ",".join(sorted(turn.origin.capability_ids))
            data["connector_set"] = ids[:1000]
        if turn.agent_run_id is None:
            return data
        data["agent_run_id"] = turn.agent_run_id
        try:
            run = self._runs.get_scoped(
                turn.agent_run_id, thread.org_id, thread.workspace_id
            )
        except KeyError:
            return data
        data["agent_id"] = run.agent_id
        data["model_id"] = run.resolved_model_id[:200]
        return data

    # -- settling --------------------------------------------------------------

    def _settled(self, event: InboxEvent, outcome: str, value: int | None) -> int:
        if not self._store.settle_event(event.event_id, "done"):
            return 0
        SLACK_FEEDBACK.inc(value=_label(value), outcome=outcome)
        logger.info(
            "slack_feedback event=%s request=%s outcome=%s value=%s",
            event.event_id,
            request_id_of(event),
            outcome,
            _label(value),
        )
        return 1

    def _drop(self, event: InboxEvent, reason: str, value: int | None) -> int:
        if not self._store.settle_event(event.event_id, "dropped", reason=reason):
            return 0
        SLACK_FEEDBACK.inc(value=_label(value), outcome=f"dropped_{reason}"[:40])
        return 1


def _label(value: int | None) -> str:
    return {1: "up", -1: "down"}.get(value or 0, "none")


def _reason(decision: Decision) -> str:
    return decision.reason.value if decision.reason else "dropped"
