"""The Ask path of the dispatcher: gates, then a hidden Q&A task, then the thread row.

One Slack thread is one conversation: the first Ask creates a private task owned
by the organization's Slack service principal, and a later mention in the same
thread appends a message to it (the model sees the earlier answers). The turn
carries ``TurnOrigin(kind="slack_qa")``; the worker guard reads it at pickup and
runs the pipeline. Everything here is idempotent: the task key is
``slack:{team}:{event_id}`` and the thread row key is unique, so a replayed event
(a crash before the inbox row was settled) adds nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Lock

from application.ports.slack import InboxEvent, SlackInstallation
from contracts.v1 import TaskAudience, TurnOrigin
from contracts.v1.slack_threads import SlackThread
from modules.limits.errors import LimitExceeded
from modules.slack.access import messages
from modules.slack.access.models import Action, Outcome
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.dispatch.qa_agent import NoModel, QaProvisioner, QaUnavailable
from modules.slack.dispatch.threads import ThreadBook
from modules.tasks.service import TaskService
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack")
TASK_TITLE = "Slack question"


class AskKind(StrEnum):
    """What happened to one Ask mention."""

    CREATED = "created"
    FOLLOW_UP = "follow_up"
    REPLAY = "replay"
    DROPPED = "dropped"
    REFUSED = "refused"
    BUSY = "busy"
    NO_MODEL = "no_model"
    UNAVAILABLE = "unavailable"
    LIMITED = "limited"


@dataclass(frozen=True)
class AskResult:
    """The dispatcher's view: an outcome label and (for refusals) the text."""

    kind: AskKind
    message: str | None = None
    reason: str = ""

    @property
    def created_work(self) -> bool:
        """A queued turn exists for this mention."""
        return self.kind in (AskKind.CREATED, AskKind.FOLLOW_UP, AskKind.REPLAY)


def idempotency_key(team_id: str, event_id: str) -> str:
    """The task (and follow-up turn) key of one Slack event."""
    return f"slack:{team_id}:{event_id}"


def request_id_of(event: InboxEvent) -> str:
    """The end-to-end request id: ``slack-{event_id}`` (design 3.11)."""
    return event.request_id or f"slack-{event.event_id}"


class AskFlow:
    """Turn one Ask mention into a queued hidden Q&A turn."""

    def __init__(
        self,
        *,
        gates: DispatchGates,
        qa: QaProvisioner,
        tasks: TaskService,
        book: ThreadBook,
    ) -> None:
        """Bind the gates, the Q&A provisioning, the task service and thread rows."""
        self._gates = gates
        self._qa = qa
        self._tasks = tasks
        self._book = book
        self._lock = Lock()

    def run(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        question: str,
        now: datetime,
    ) -> AskResult:
        """Gate, provision and queue; never calls the model or Slack."""
        channel, user = event.channel_id or "", event.user_id or ""
        decision = self._gates.evaluate(
            installation,
            slack_user_id=user,
            channel_id=channel,
            action=Action.ASK,
            now=now,
            question_chars=len(question),
        )
        if not decision.allowed:
            return _refusal(decision.outcome, decision.reason, decision.message)
        try:
            target = self._qa.ensure(installation.org_id, installation.workspace_id)
        except NoModel:
            return AskResult(AskKind.NO_MODEL)
        except QaUnavailable:
            return AskResult(
                AskKind.UNAVAILABLE, messages.UNAVAILABLE, "qa_unavailable"
            )
        token = set_request_identity(
            target.principal_id, installation.org_id, installation.workspace_id
        )
        try:
            # One dispatcher thread per process creates at a time; across processes
            # the database's unique task key is the guard.
            with self._lock, extend_context(request_id=request_id_of(event)):
                try:
                    return self._queue(installation, event, question, target.agent_id, now)
                except PermissionError as denied:  # TEMP-DIAG
                    org = installation.org_id
                    mine = [
                        (b.workspace_id, [str(p) for p in self._qa._roles.get_scoped(b.role_id, org).permissions])
                        for b in self._qa._bindings.list_scoped(org)
                        if b.principal_id == target.principal_id
                    ]
                    raise RuntimeError(
                        f"{denied} inst_ws={installation.workspace_id} principal={target.principal_id} bindings={mine} qa_ids={self._qa._access.qa_ids()}"
                    ) from denied
        except LimitExceeded as error:
            text = (
                messages.rate_limited(error.retry_after_seconds)
                if error.retry_after_seconds
                else messages.UNAVAILABLE
            )
            return AskResult(AskKind.LIMITED, text, error.resource[:40])
        finally:
            reset_request_principal(token)

    def _origin(
        self, installation: SlackInstallation, event: InboxEvent, thread_ts: str
    ) -> TurnOrigin:
        return TurnOrigin(
            kind="slack_qa",
            team_id=installation.team_id,
            channel_id=event.channel_id or "",
            thread_ts=thread_ts,
            slack_user_id=event.user_id or "",
            event_id=event.event_id,
            settings_version=self._gates.settings_version(),
            action="ask",
        )

    def _queue(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        question: str,
        agent_id: str,
        now: datetime,
    ) -> AskResult:
        channel = event.channel_id or ""
        thread_ts = event.thread_ts or event.message_ts or ""
        org = installation.org_id
        row = self._book.find(org, installation.team_id, channel, thread_ts)
        if row is not None and row.event_id == event.event_id:
            return AskResult(AskKind.REPLAY)
        if row is not None and row.reply_state == "pending":
            return AskResult(AskKind.BUSY, reason="reply_pending")
        origin = self._origin(installation, event, thread_ts)
        key = idempotency_key(installation.team_id, event.event_id)
        if row is None:
            return self._start(
                installation, event, question, agent_id, origin, key, now
            )
        try:
            turn = self._tasks.add_message(
                row.task_id, question, idempotency_key=key, origin=origin
            )
        except ValueError:
            return AskResult(AskKind.BUSY, reason="turn_active")
        self._book.renew(
            row,
            turn_id=turn.turn_id,
            event_id=event.event_id,
            request_id=request_id_of(event),
            message_ts=event.message_ts or thread_ts,
            slack_user_id=event.user_id or "",
            now=now,
        )
        return AskResult(AskKind.FOLLOW_UP)

    def _start(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        question: str,
        agent_id: str,
        origin: TurnOrigin,
        key: str,
        now: datetime,
    ) -> AskResult:
        task = self._tasks.create_task(
            agent_id=agent_id,
            title=TASK_TITLE,
            initial_message=question,
            idempotency_key=key,
            audience=TaskAudience.PRIVATE,
            origin=origin,
        )
        channel = event.channel_id or ""
        thread_ts = origin.thread_ts
        principal = task.created_by_principal_id
        row = SlackThread(
            id=self._book.key(
                installation.org_id, installation.team_id, channel, thread_ts
            ),
            org_id=installation.org_id,
            workspace_id=installation.workspace_id,
            team_id=installation.team_id,
            channel_id=channel,
            thread_ts=thread_ts,
            task_id=task.task_id,
            requester_slack_user_id=event.user_id or "",
            requester_principal_id=principal,
            turn_id=task.active_turn_id or "",
            event_id=event.event_id,
            request_id=request_id_of(event),
            message_ts=event.message_ts or thread_ts,
            requested_at=now,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        if self._book.start(row) is None:  # a concurrent dispatcher won the race
            return AskResult(AskKind.REPLAY)
        return AskResult(AskKind.CREATED)


def _refusal(outcome: Outcome, reason: object, message: str | None) -> AskResult:
    code = getattr(reason, "value", "") if reason is not None else ""
    if outcome is Outcome.DROPPED:
        return AskResult(AskKind.DROPPED, None, code)
    if outcome is Outcome.RATE_LIMITED:
        return AskResult(AskKind.LIMITED, message or messages.UNAVAILABLE, code)
    return AskResult(AskKind.REFUSED, message or messages.UNAVAILABLE, code)
