"""The messages of a Slack-created task (scene C): created, result, receipt.

One request makes at most two bot posts, in this order, each stored on the thread
row the moment Slack accepts it so a retry or a restart never posts it twice:

1. ``Task created: <title> · Agent: <name> · Running · I'll post the result here``
   (posted as soon as the thread row exists; it is the row's ``status_ts``);
2. when the task's turn finished, the result as a **new** message (the asker is
   notified): a short summary, up to five bullets, ``Full result and sources in
   Weaves.`` and an ``Open result`` button; ``answer_ts``;
3. the first message is then **edited** into the receipt ``Task done: <title> ·
   <agent> · 4m 12s`` with a ``View in Weaves`` button.

A failed, refused or stopped task makes no second post: the first message is
edited into the plain failure line. Nothing here logs or stores message text;
titles and agent names are sanitized like any other text that reaches Slack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from application.ports.repository import Repository
from application.ports.slack import SlackInstallation
from contracts.v1 import AgentDefinition, Task, TaskMessage, TaskTurn, TaskTurnStatus
from contracts.v1.slack_threads import SlackThread
from modules.slack import copy
from modules.slack.dispatch.threads import ThreadBook
from modules.slack.reply.compose import short_ref
from modules.slack.reply.poster import LinkButton, PostError, SlackPoster
from modules.slack.tasks.links import TaskLinks
from modules.slack.tasks.text import result_text
from modules.slack.text.mrkdwn import to_mrkdwn
from shared.metrics import SLACK_REPLY, SLACK_REPLY_SECONDS

logger = logging.getLogger("product.slack")
MAX_POSTS = 2


@dataclass(frozen=True)
class TaskLabels:
    """What the receipt shows about the task (read once per step)."""

    title: str
    agent: str


def _safe(text: str) -> str:
    """One sanitized line of plain words (mentions neutralized, no markup kept)."""
    return " ".join(to_mrkdwn(text).split())[:120] or "-"


class TaskReply:
    """Post and edit the messages of one task thread row."""

    def __init__(
        self,
        *,
        poster: SlackPoster,
        book: ThreadBook,
        tasks: Repository[Task],
        agents: Repository[AgentDefinition],
        task_messages: Repository[TaskMessage],
        links: TaskLinks,
    ) -> None:
        """Bind the poster, the thread rows and the records the messages read."""
        self._poster = poster
        self._book = book
        self._tasks = tasks
        self._agents = agents
        self._messages = task_messages
        self._links = links

    def needs_work(self, row: SlackThread, turn: TaskTurn) -> bool:
        """Whether this row has anything to post or edit right now."""
        if row.status_ts is None:
            return True
        return turn.status not in (TaskTurnStatus.QUEUED, TaskTurnStatus.RUNNING)

    def step(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        turn: TaskTurn,
        now: datetime,
    ) -> SlackThread:
        """Do the next unfinished step; returns the saved row (raises ``PostError``)."""
        labels = self._labels(row)
        button = self._button(row.task_id, copy.VIEW_IN_WEAVES)
        if row.status_ts is None:
            row = self._post_created(installation, row, labels, button, now)
        if turn.status in (TaskTurnStatus.QUEUED, TaskTurnStatus.RUNNING):
            return self._book.save(
                row.model_copy(update={"next_attempt_at": None}), now
            )
        return self._finish(installation, row, turn, labels, button, now)

    # -- the three messages ---------------------------------------------------------

    def _post_created(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        labels: TaskLabels,
        button: LinkButton | None,
        now: datetime,
    ) -> SlackThread:
        ts = self._poster.post(
            installation,
            row.channel_id,
            copy.task_created(labels.title, labels.agent),
            thread_ts=row.thread_ts,
            rich=True,
            button=button,
        )
        SLACK_REPLY.inc(kind="task_created", outcome="posted")
        return self._book.save(
            row.model_copy(
                update={
                    "status_ts": ts,
                    "posts": row.posts + 1,
                    "run_state": "running",
                    "status_text_key": "task",
                }
            ),
            now,
        )

    def _finish(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        turn: TaskTurn,
        labels: TaskLabels,
        button: LinkButton | None,
        now: datetime,
    ) -> SlackThread:
        succeeded = (
            turn.status is TaskTurnStatus.SUCCEEDED and turn.agent_run_id is not None
        )
        if not succeeded:
            return self._close_unfinished(installation, row, turn, labels, now)
        if row.answer_ts is None:
            row = self._post_result(installation, row, turn, now)
        elapsed = ((turn.finished_at or now) - row.requested_at).total_seconds()
        self._edit_first(
            installation,
            row,
            copy.task_done(labels.title, labels.agent, copy.duration(elapsed)),
            button,
        )
        SLACK_REPLY.inc(kind="task_receipt", outcome="edited")
        SLACK_REPLY_SECONDS.observe(max(0.0, elapsed))
        return self._settle(row, "done", turn, now)

    def _post_result(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        turn: TaskTurn,
        now: datetime,
    ) -> SlackThread:
        if row.posts >= MAX_POSTS:
            raise PostError("permanent", "post_budget")
        ts = self._poster.post(
            installation,
            row.channel_id,
            self._result_body(turn),
            thread_ts=row.thread_ts,
            rich=True,
            button=self._button(row.task_id, copy.OPEN_RESULT),
        )
        SLACK_REPLY.inc(kind="task_result", outcome="posted")
        return self._book.save(
            row.model_copy(update={"answer_ts": ts, "posts": row.posts + 1}), now
        )

    def _close_unfinished(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        turn: TaskTurn,
        labels: TaskLabels,
        now: datetime,
    ) -> SlackThread:
        """Failed, refused or stopped: the first message becomes the plain reason."""
        verdict = turn.origin.verdict if turn.origin else None
        if turn.status is TaskTurnStatus.CANCELLED and verdict is None:
            text, state = copy.TASK_STOPPED, "stopped"
        elif verdict is not None and verdict.message:
            text, state = copy.refusal(verdict.message, verdict.outcome), "failed"
        elif turn.error is not None and turn.error.code.startswith("model."):
            text, state = copy.NO_MODEL, "failed"
        else:
            text = copy.task_failed(labels.title, short_ref(turn.turn_id))
            state = "failed"
        self._edit_first(
            installation, row, text, self._button(row.task_id, copy.VIEW_IN_WEAVES)
        )
        SLACK_REPLY.inc(kind="task_closed", outcome=state)
        return self._settle(row, state, turn, now)

    # -- helpers --------------------------------------------------------------------

    def _edit_first(
        self,
        installation: SlackInstallation,
        row: SlackThread,
        text: str,
        button: LinkButton | None,
    ) -> None:
        if row.status_ts is None:
            return
        self._poster.edit(
            installation, row.channel_id, row.status_ts, text, rich=True, button=button
        )

    def _settle(
        self, row: SlackThread, state: str, turn: TaskTurn, now: datetime
    ) -> SlackThread:
        return self._book.save(
            row.model_copy(
                update={
                    "run_state": state,
                    "reply_state": "posted",
                    "finished_at": turn.finished_at or now,
                    "next_attempt_at": None,
                    "reply_attempts": 0,
                    "reply_error": "",
                }
            ),
            now,
        )

    def _result_body(self, turn: TaskTurn) -> str:
        try:
            content = self._messages.get_scoped(
                turn.assistant_message_id or "", turn.org_id, turn.workspace_id
            ).content
        except KeyError:
            content = ""
        return result_text(content)

    def _button(self, task_id: str, label: str) -> LinkButton | None:
        url = self._links.task_url(task_id)
        return LinkButton(label, url) if url else None

    def _labels(self, row: SlackThread) -> TaskLabels:
        try:
            task = self._tasks.get_scoped(row.task_id, row.org_id, row.workspace_id)
        except KeyError:
            return TaskLabels(copy.TASK_DEFAULT_TITLE, "agent")
        try:
            agent = self._agents.get_scoped(
                task.agent_id, task.org_id, task.workspace_id
            ).name
        except KeyError:
            agent = "agent"
        return TaskLabels(_safe(task.title), _safe(agent))
