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
from shared.clock import utc_now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class TaskLifecycleMixin:
    """Provide task lifecycle transitions with audience-aware authorization."""

    tasks: Repository[Task]
    turns: Repository[TaskTurn]
    _authorize: Callable[[str, Permission], None]
    _open_unit_of_work: Callable[[], AbstractContextManager[None]]
    _ensure_ready: Callable[[], None]
    get_task: Callable[[str], Task]

    def cancel_turn(self, task_id: str, turn_id: str) -> None:
        """Cancel a workflow child task and revoke its active worker lease."""
        self._authorize(
            effective_principal_id("local-developer"), Permission.WORKFLOWS_RUN
        )
        with self._open_unit_of_work():
            task = self.tasks.get_scoped(
                task_id, effective_org_id(), effective_workspace_id()
            )
            if (
                task.audience is TaskAudience.PRIVATE
                and task.audience_owner_principal_id
                != effective_principal_id("local-developer")
            ):
                raise KeyError("task not found")
            turn = self.turns.get_scoped(turn_id, task.org_id, task.workspace_id)
            if turn.task_id != task_id or task.active_turn_id != turn_id:
                return
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
                        "updated_at": now,
                    }
                ).model_dump()
            )
            self.tasks.put_if_task_state(
                updated,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=turn_id,
            )

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
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
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
