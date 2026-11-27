"""The ``slack_retention`` worker tick: hidden Q&A tasks and inbox rows expire.

A hidden Q&A task exists only because the worker needs a task to run a turn; it is
not a record anyone browses. Thirty days after a thread's last request (ASSUMPTION,
``WEAVES_SLACK_QA_RETENTION_DAYS``) the whole conversation is deleted: the task,
its thread, messages and turns, and every run output that holds text (agent runs,
workflow runs, tool invocations, artifacts), then the ``slack_threads`` row. The
inbox keeps nothing of a mention once it was handled, and processed rows go after
seven days. A request that is still being answered is never deleted.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any

from application.ports.repository import Repository
from application.ports.slack import SlackStore
from contracts.v1 import (
    AgentRun,
    Artifact,
    ConversationThread,
    Task,
    TaskMessage,
    TaskTurn,
    ToolInvocation,
    WorkflowRun,
)
from contracts.v1.slack_threads import SlackThread
from modules.slack.events import tenant_of
from shared.clock import utc_now
from shared.metrics import SLACK_RETENTION

logger = logging.getLogger("product.slack")
DEFAULT_QA_DAYS = 30
BATCH_PER_ORG = 50
TICK_BUDGET_SECONDS = 20.0


def qa_retention_days(env: dict[str, str] | None = None) -> int:
    """``WEAVES_SLACK_QA_RETENTION_DAYS`` (1 to 365), default 30."""
    raw = (env if env is not None else os.environ).get("WEAVES_SLACK_QA_RETENTION_DAYS")
    try:
        days = int(raw) if raw else DEFAULT_QA_DAYS
    except ValueError:
        return DEFAULT_QA_DAYS
    return days if 1 <= days <= 365 else DEFAULT_QA_DAYS


def _remove(repository: Repository[Any], record_id: str) -> None:
    """Delete one record; one that is already gone is not an error."""
    try:
        repository.delete(record_id)
    except KeyError:
        return


class SlackRetention:
    """Delete expired hidden conversations and processed inbox rows."""

    def __init__(
        self,
        *,
        store: SlackStore,
        threads: Repository[SlackThread],
        tasks: Repository[Task],
        conversation_threads: Repository[ConversationThread],
        messages: Repository[TaskMessage],
        turns: Repository[TaskTurn],
        agent_runs: Repository[AgentRun],
        runs: Repository[WorkflowRun],
        invocations: Repository[ToolInvocation],
        artifacts: Repository[Artifact],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        enabled: Callable[[], bool],
        qa_days: int = DEFAULT_QA_DAYS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind every collection a conversation touches."""
        self._store = store
        self._threads = threads
        self._tasks = tasks
        self._conversations = conversation_threads
        self._messages = messages
        self._turns = turns
        self._agent_runs = agent_runs
        self._runs = runs
        self._invocations = invocations
        self._artifacts = artifacts
        self._uow = unit_of_work
        self._enabled = enabled
        self._qa_days = qa_days
        self._clock = clock

    def run(self) -> int:
        """Purge a bounded batch; returns the number of rows removed."""
        now = self._clock()
        started = time.monotonic()
        removed = 0
        if not self._enabled():
            return removed
        cutoff = now - timedelta(days=self._qa_days)
        for held in self._store.list_held():
            if time.monotonic() - started > TICK_BUDGET_SECONDS:
                break
            with tenant_of(held):
                removed += self._purge_org(held.org_id, held.team_id, cutoff)
        return removed

    def _purge_org(self, org_id: str, team_id: str, cutoff: datetime) -> int:
        # Only hidden Q&A conversations expire; a created task is a normal task
        # (and its thread row is what keeps "Already tracked" working).
        expired = self._threads.list_before(
            "updated_at", cutoff, limit=BATCH_PER_ORG, equals={"kind": "ask"}
        )
        count = 0
        for row in expired:
            if row.org_id != org_id or row.team_id != team_id:
                continue
            try:
                self._delete_conversation(row)
            except Exception as error:
                logger.error("slack_retention_failed kind=%s", type(error).__name__)
                continue
            count += 1
            SLACK_RETENTION.inc(kind="qa_task")
        return count

    def _delete_conversation(self, row: SlackThread) -> None:
        org, workspace = row.org_id, row.workspace_id
        with self._uow():
            turns = self._turns.list_by_field("task_id", row.task_id)
            for turn in turns:
                self._delete_run_outputs(turn)
                _remove(self._turns, turn.turn_id)
            for message in self._messages.list_by_field("task_id", row.task_id):
                _remove(self._messages, message.message_id)
            try:
                task = self._tasks.get_scoped(row.task_id, org, workspace)
            except KeyError:
                task = None
            if task is not None:
                _remove(self._tasks, task.task_id)
                _remove(self._conversations, task.thread_id)
            _remove(self._threads, row.id)

    def _delete_run_outputs(self, turn: TaskTurn) -> None:
        if turn.agent_run_id is None:
            return
        try:
            agent_run = self._agent_runs.get_scoped(
                turn.agent_run_id, turn.org_id, turn.workspace_id
            )
        except KeyError:
            return
        for item in self._invocations.list_by_field(
            "agent_run_id", agent_run.agent_run_id
        ):
            _remove(self._invocations, item.invocation_id)
        for artifact in self._artifacts.list_by_field(
            "agent_run_id", agent_run.agent_run_id
        ):
            _remove(self._artifacts, artifact.artifact_id)
        _remove(self._agent_runs, agent_run.agent_run_id)
        _remove(self._runs, agent_run.run_id)
