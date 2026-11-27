"""The ``task:`` path of the dispatcher: gates, then a normal task as the member.

``@Weaves task: <text>`` is an Execute action (design F2, scene C). The engine
decides first (state, Execute rule, the mapped active member's permissions, the
Execute limits); a blocked request makes no task. An allowed one becomes a
**normal** workspace task created through ``TaskService.create_task`` as the
mapped member's own principal, so RBAC, admission and audit use the same paths as
a task made in the dashboard. The thread row (kind ``task``) maps the Slack
thread to it; a second ``task:`` in the same thread finds that row and answers
"Already tracked" instead of creating another.

Everything is idempotent: the task key is ``slack:{team}:{event_id}``, the thread
row key is unique, and the whole step runs under one advisory lock per thread, so
two dispatchers (or a replay after a crash) never create two tasks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from application.ports.repository import Repository
from application.ports.slack import InboxEvent, SlackInstallation
from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    Task,
    TaskAudience,
    TaskOrigin,
    TurnOrigin,
)
from contracts.v1.slack_threads import SlackThread
from modules.limits.errors import LimitExceeded
from modules.slack import copy
from modules.slack.access import messages
from modules.slack.access.allowlists import Allowlists
from modules.slack.access.decision_audit import DecisionContext, DecisionRecorder
from modules.slack.access.models import Action, Decision, Outcome, Reason
from modules.slack.dispatch.ask_flow import idempotency_key, request_id_of
from modules.slack.dispatch.gates import DispatchGates
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.tasks.links import TaskLinks
from modules.slack.tasks.text import title_of
from modules.tasks.service import TaskService
from shared.clock import utc_now
from shared.correlation import extend_context
from shared.metrics import SLACK_EXECUTE
from shared.request_context import reset_request_principal, set_request_identity

logger = logging.getLogger("product.slack")
KIND = "task"


class TaskKind(StrEnum):
    """What happened to one ``task:`` mention."""

    CREATED = "created"
    REPLAY = "replay"
    TRACKED = "tracked"
    NEEDS_TEXT = "needs_text"
    REFUSED = "refused"
    NO_AGENT = "no_agent"
    LIMITED = "limited"
    DROPPED = "dropped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TaskResult:
    """The dispatcher's view: an outcome, the text to post, and an optional link."""

    kind: TaskKind
    message: str | None = None
    reason: str = ""
    button_label: str | None = None
    button_url: str | None = None

    @property
    def created_work(self) -> bool:
        """A queued task turn exists for this mention."""
        return self.kind in (TaskKind.CREATED, TaskKind.REPLAY)

    @property
    def public(self) -> bool:
        """Whether the reply goes to the thread (else it is private to the asker)."""
        return self.kind in (TaskKind.TRACKED, TaskKind.NEEDS_TEXT)


def _refusal_text(decision: Decision) -> str:
    """Scene E for a switched-off rule, else the engine's own list-free text."""
    if decision.reason is Reason.RULE_OFF:
        return copy.refusal(copy.NOT_SET_UP["task"], Outcome.BLOCKED_RULE.value)
    text = decision.message or messages.UNAVAILABLE
    return copy.refusal(text, decision.outcome.value)


class TaskFlow:
    """Turn one ``task:`` mention into a task and its thread row."""

    def __init__(
        self,
        *,
        gates: DispatchGates,
        tasks: TaskService,
        task_records: Repository[Task],
        agents: Repository[AgentDefinition],
        book: ThreadBook,
        recorder: DecisionRecorder,
        allowlists: Allowlists,
        links: TaskLinks,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        lock: Callable[[str], None] | None,
    ) -> None:
        """Bind the gates, the task service and the records the flow reads."""
        self._gates = gates
        self._tasks = tasks
        self._records = task_records
        self._agents = agents
        self._book = book
        self._recorder = recorder
        self._allowlists = allowlists
        self._links = links
        self._uow = unit_of_work
        self._lock = lock

    def run(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        text: str,
        now: datetime,
    ) -> TaskResult:
        """Gate, create and record; never calls Slack or the model."""
        channel, user = event.channel_id or "", event.user_id or ""
        decision = self._gates.evaluate(
            installation,
            slack_user_id=user,
            channel_id=channel,
            action=Action.CREATE_TASK,
            now=now,
            event_id=event.event_id,
        )
        context = self._context(installation, event, user, channel)
        if not decision.allowed:
            return self._blocked(decision, context, now)
        if not text.strip():
            return TaskResult(TaskKind.NEEDS_TEXT, copy.MISSING_TASK_TEXT)
        thread_ts = event.thread_ts or event.message_ts or ""
        tracked = self._book.find(
            installation.org_id, installation.team_id, channel, thread_ts, KIND
        )
        if tracked is not None:  # the thread has its task: no agent needed to say so
            return self._existing(tracked, event)
        agent = self._agent(installation)
        if agent is None:
            return TaskResult(TaskKind.NO_AGENT, copy.NO_TASK_AGENT, "no_task_agent")
        member = self._gates.member(user, channel)
        if member is None:  # the engine only allows a mapped member; fail closed
            return TaskResult(TaskKind.REFUSED, messages.NOT_MAPPED, "member_missing")
        token = set_request_identity(
            member.principal_id, installation.org_id, installation.workspace_id
        )
        try:
            with extend_context(request_id=request_id_of(event)), self._uow():
                if self._lock is not None:
                    self._lock(f"weaves-slack-task:{channel}:{thread_ts}")
                return self._create(
                    installation, event, text, agent, decision, context, now
                )
        except LimitExceeded as error:
            wait = error.retry_after_seconds
            text_out = messages.rate_limited(wait) if wait else messages.UNAVAILABLE
            return TaskResult(
                TaskKind.LIMITED, copy.refusal(text_out, "rate_limited"), "limit"
            )
        finally:
            reset_request_principal(token)

    # -- gates ------------------------------------------------------------------

    @staticmethod
    def _context(
        installation: SlackInstallation, event: InboxEvent, user: str, channel: str
    ) -> DecisionContext:
        return DecisionContext(
            org_id=installation.org_id,
            event_id=event.event_id,
            request_id=request_id_of(event),
            slack_user_id=user,
            channel_id=channel,
        )

    def _blocked(
        self, decision: Decision, context: DecisionContext, now: datetime
    ) -> TaskResult:
        code = decision.reason.value if decision.reason else ""
        if decision.outcome is Outcome.DROPPED:
            return TaskResult(TaskKind.DROPPED, None, code)
        if decision.outcome in (Outcome.BLOCKED_RULE, Outcome.RATE_LIMITED):
            self._recorder.record(decision, context, now)
        SLACK_EXECUTE.inc(action="create_task", outcome=decision.outcome.value)
        kind = (
            TaskKind.LIMITED
            if decision.outcome is Outcome.RATE_LIMITED
            else TaskKind.REFUSED
        )
        return TaskResult(kind, _refusal_text(decision), code)

    def _agent(self, installation: SlackInstallation) -> AgentDefinition | None:
        """The default task agent, only while it is still an active agent."""
        agent_id = self._gates.default_task_agent_id()
        if agent_id is None:
            return None
        try:
            agent = self._agents.get_scoped(
                agent_id, installation.org_id, installation.workspace_id
            )
        except KeyError:
            return None
        return agent if agent.status is AgentStatus.ACTIVE else None

    # -- creation ------------------------------------------------------------------

    def _create(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        text: str,
        agent: AgentDefinition,
        decision: Decision,
        context: DecisionContext,
        now: datetime,
    ) -> TaskResult:
        channel = event.channel_id or ""
        thread_ts = event.thread_ts or event.message_ts or ""
        org = installation.org_id
        row = self._book.find(org, installation.team_id, channel, thread_ts, KIND)
        if row is not None:
            return self._existing(row, event)
        member = self._gates.member(event.user_id or "", channel)
        assert member is not None
        task = self._tasks.create_task(
            agent_id=agent.id,
            title=title_of(text),
            initial_message=text.strip(),
            idempotency_key=idempotency_key(installation.team_id, event.event_id),
            audience=TaskAudience.WORKSPACE,
            origin=self._turn_origin(installation, event, thread_ts),
            task_origin=self._task_origin(installation, event, member.principal_id),
        )
        saved = self._book.start(
            self._row(installation, event, task, member.principal_id, thread_ts, now)
        )
        if saved is None:  # a concurrent dispatcher won the race (the lock prevents it)
            return TaskResult(TaskKind.REPLAY)
        self._recorder.record_execute_attempt(
            decision, replace(context, principal_id=member.principal_id), now
        )
        SLACK_EXECUTE.inc(action="create_task", outcome="created")
        return TaskResult(TaskKind.CREATED)

    def _existing(self, row: SlackThread, event: InboxEvent) -> TaskResult:
        """The thread already has its task: the same event is a replay."""
        if row.event_id == event.event_id:
            return TaskResult(TaskKind.REPLAY)
        try:
            task = self._records.get_scoped(row.task_id, row.org_id, row.workspace_id)
            title = task.title
        except KeyError:
            title = copy.TASK_DEFAULT_TITLE
        url = self._links.task_url(row.task_id)
        SLACK_EXECUTE.inc(action="create_task", outcome="already_tracked")
        return TaskResult(
            TaskKind.TRACKED,
            copy.already_tracked(title),
            "already_tracked",
            copy.VIEW_TASK if url else None,
            url,
        )

    def _turn_origin(
        self, installation: SlackInstallation, event: InboxEvent, thread_ts: str
    ) -> TurnOrigin:
        return TurnOrigin(
            kind="slack",
            team_id=installation.team_id,
            channel_id=event.channel_id or "",
            thread_ts=thread_ts,
            slack_user_id=event.user_id or "",
            event_id=event.event_id,
            settings_version=self._gates.settings_version(),
            action="create_task",
        )

    def _task_origin(
        self, installation: SlackInstallation, event: InboxEvent, principal_id: str
    ) -> TaskOrigin:
        """The public summary on the task (see ``TaskOrigin`` for what it omits)."""
        channel = event.channel_id or ""
        thread_ts = event.thread_ts or event.message_ts or ""
        listed = self._allowlists.get_channel(channel)
        name = (listed.name_cached.strip() if listed is not None else "") or None
        return TaskOrigin(
            kind="slack",
            channel_id=channel,
            channel_name=name,
            requested_by_name=(
                self._allowlists.members.display_name(principal_id) or None
            ),
            permalink=self._links.thread_permalink(installation, channel, thread_ts),
        )

    def _row(
        self,
        installation: SlackInstallation,
        event: InboxEvent,
        task: Task,
        principal_id: str,
        thread_ts: str,
        now: datetime,
    ) -> SlackThread:
        channel = event.channel_id or ""
        return SlackThread(
            id=self._book.key(
                installation.org_id, installation.team_id, channel, thread_ts, KIND
            ),
            org_id=installation.org_id,
            workspace_id=installation.workspace_id,
            team_id=installation.team_id,
            channel_id=channel,
            thread_ts=thread_ts,
            kind="task",
            tier="execute",
            task_id=task.task_id,
            requester_slack_user_id=event.user_id or "",
            requester_principal_id=principal_id,
            turn_id=task.active_turn_id or "",
            event_id=event.event_id,
            request_id=request_id_of(event),
            message_ts=event.message_ts or thread_ts,
            requested_at=now,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
