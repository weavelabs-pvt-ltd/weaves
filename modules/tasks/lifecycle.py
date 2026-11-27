"""Task lifecycle transitions and private task access policy."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    Permission,
    Task,
    TaskAudience,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
)
from modules.limits.service import AdmissionService
from shared.clock import utc_now
from shared.errors import ConfigurationRejected
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)

TURN_NOT_RETRYABLE = "task.turn_not_retryable"


class _TurnAlreadyFinished(Exception):
    """A worker finished the turn while it was being cancelled (roll back)."""


class TaskLifecycleMixin:
    """Provide task lifecycle transitions with audience-aware authorization."""

    tasks: Repository[Task]
    turns: Repository[TaskTurn]
    _authorize: Callable[[str, Permission], None]
    _open_unit_of_work: Callable[[], AbstractContextManager[None]]
    _ensure_ready: Callable[[], None]
    get_task: Callable[[str], Task]
    admission: AdmissionService

    def cancel_turn(self, task_id: str, turn_id: str) -> None:
        """Cancel a workflow child task and revoke its active worker lease."""
        self._authorize(
            effective_principal_id("local-developer"), Permission.WORKFLOWS_RUN
        )
        self._cancel_active_turn(
            task_id,
            turn_id,
            owner=effective_principal_id("local-developer"),
            origin_only=False,
        )

    def cancel_origin_turn(self, task_id: str, turn_id: str) -> bool:
        """Cancel an active turn that came from an outside channel (Slack Stop).

        For internal callers that have already authorized the person (the Slack
        press check): no permission is checked here, and only a turn that carries
        an ``origin`` can be cancelled this way. The worker sees the cancelled
        status at its next stage boundary, drops what it was reading and discards
        any late answer (its lease is gone). Returns whether a turn was cancelled;
        ``False`` when it had already finished.
        """
        return self._cancel_active_turn(task_id, turn_id, owner=None, origin_only=True)

    def _cancel_active_turn(
        self, task_id: str, turn_id: str, *, owner: str | None, origin_only: bool
    ) -> bool:
        try:
            with self._open_unit_of_work():
                return self._cancel_in_transaction(
                    task_id, turn_id, owner=owner, origin_only=origin_only
                )
        except _TurnAlreadyFinished:
            return False  # the transaction rolled back: the turn keeps its result

    def _cancel_in_transaction(
        self, task_id: str, turn_id: str, *, owner: str | None, origin_only: bool
    ) -> bool:
        task = self.tasks.get_scoped(
            task_id, effective_org_id(), effective_workspace_id()
        )
        if (
            owner is not None
            and task.audience is TaskAudience.PRIVATE
            and task.audience_owner_principal_id != owner
        ):
            raise KeyError("task not found")
        turn = self.turns.get_scoped(turn_id, task.org_id, task.workspace_id)
        if turn.task_id != task_id or task.active_turn_id != turn_id:
            return False
        if origin_only and turn.origin is None:
            return False
        now = utc_now()
        cancelled = TaskTurn.model_validate(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.CANCELLED,
                    "worker_id": None,
                    "finished_at": now,
                    "updated_at": now,
                    "error": None,
                }
            ).model_dump()
        )
        self.turns.put(cancelled)
        updated = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_id": None,
                    "active_turn_status": None,
                    "latest_turn_status": TaskTurnStatus.CANCELLED,
                    "latest_turn_error": None,
                    "updated_at": now,
                }
            ).model_dump()
        )
        if not self.tasks.put_if_task_state(
            updated,
            expected_status=TaskStatus.OPEN.value,
            expected_active_turn_id=turn_id,
        ):
            raise _TurnAlreadyFinished
        return True

    def complete_task(self, task_id: str) -> Task:
        """Mark an idle task completed; its history remains available."""
        return self._change_status(task_id, TaskStatus.COMPLETED)

    def retry_turn(self, task_id: str, turn_id: str) -> TaskTurn:
        """Requeue a failed turn using a fresh agent-run idempotency key."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize(principal, Permission.TASKS_CREATE)
        self._authorize(principal, Permission.AGENTS_RUN)
        task = self.get_task(task_id)
        turn = self.turns.get_scoped(turn_id, task.org_id, task.workspace_id)
        if turn.task_id != task.task_id:
            raise KeyError(turn_id)
        if turn.status is not TaskTurnStatus.FAILED:
            raise ValueError("only failed task turns can be retried")
        if turn.error is not None and not turn.error.retryable:
            # The task keeps its agent version, so a retry would fail the same way.
            raise ConfigurationRejected(
                TURN_NOT_RETRYABLE,
                "This turn failed for a reason a retry cannot fix. Fix the cause, "
                "then start a new task.",
            )
        if task.status is not TaskStatus.OPEN or task.active_turn_id is not None:
            raise ValueError("only idle open tasks can retry a turn")
        now = utc_now()
        retry = TaskTurn.model_validate(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.QUEUED,
                    "run_idempotency_key": None,
                    "worker_id": None,
                    "agent_run_id": None,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "updated_at": now,
                }
            ).model_dump()
        )
        updated_task = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_id": turn_id,
                    "active_turn_status": TaskTurnStatus.QUEUED,
                    "latest_turn_id": turn_id,
                    "latest_turn_status": TaskTurnStatus.QUEUED,
                    "latest_turn_error": None,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            self.admission.admit_work(task.org_id)
            changed = self.tasks.put_if_task_state(
                updated_task,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=None,
            )
            if not changed:
                raise ValueError("only idle open tasks can retry a turn")
            self.turns.put(retry)
        return retry

    def archive_task(self, task_id: str) -> Task:
        """Archive an idle task while retaining its transcript."""
        return self._change_status(task_id, TaskStatus.ARCHIVED)

    def reopen_task(self, task_id: str) -> Task:
        """Reopen a completed task for another conversation turn."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        task = self.get_task(task_id)
        self._authorize_task_management(principal, task)
        if task.active_turn_id:
            raise ValueError("tasks with a turn in progress cannot be reopened")
        if task.status is TaskStatus.ARCHIVED:
            raise ValueError("archived tasks are read-only")
        if task.status is not TaskStatus.COMPLETED:
            raise ValueError("only completed tasks can be reopened")
        updated = Task.model_validate(
            task.model_copy(
                update={
                    "status": TaskStatus.OPEN,
                    "completed_at": None,
                    "archived_at": None,
                    "updated_at": utc_now(),
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            changed = self.tasks.put_if_task_state(
                updated,
                expected_status=TaskStatus.COMPLETED.value,
                expected_active_turn_id=None,
            )
            if not changed:
                raise ValueError("task state changed; refresh and try again")
        return updated

    def _change_status(self, task_id: str, status: TaskStatus) -> Task:
        """Apply a terminal user lifecycle state to an idle task."""
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        task = self.get_task(task_id)
        self._authorize_task_management(principal, task)
        if task.active_turn_id:
            raise ValueError("tasks with a turn in progress cannot be closed")
        if task.status is not TaskStatus.OPEN:
            raise ValueError("only open tasks can be completed or archived")
        now = utc_now()
        changes = {"status": status, "updated_at": now}
        changes["completed_at" if status is TaskStatus.COMPLETED else "archived_at"] = (
            now
        )
        updated = Task.model_validate(task.model_copy(update=changes).model_dump())
        with self._open_unit_of_work():
            changed = self.tasks.put_if_task_state(
                updated,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=None,
            )
            if not changed:
                raise ValueError("task state changed; refresh and try again")
        return updated

    def _authorize_task_management(self, principal: str, task: Task) -> None:
        """Allow private-task owners to manage their own task lifecycle."""
        if (
            task.audience is TaskAudience.PRIVATE
            and task.audience_owner_principal_id == principal
        ):
            self._authorize(principal, Permission.TASKS_CREATE)
            return
        self._authorize(principal, Permission.TASKS_MANAGE)
