"""Persist a pre-run guard's decision on a claimed task turn.

Three outcomes of the Slack guard (``application.ports.turn_guard``): record the
verdict and the approved capabilities on a turn that proceeds, cancel a turn whose
policy no longer passes, or finish a turn with a fixed short message when a tool
was red, without ever calling the answer model.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    Task,
    TaskMessage,
    TaskMessageRole,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
    TurnOrigin,
)
from shared.clock import new_id, utc_now


class GuardedTurnWriter:
    """Write guard outcomes under the worker's lease and the task's state."""

    def __init__(
        self,
        *,
        tasks: Repository[Task],
        messages: Repository[TaskMessage],
        turns: Repository[TaskTurn],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind the task records and the transaction boundary."""
        self._tasks = tasks
        self._messages = messages
        self._turns = turns
        self._uow = unit_of_work

    def record_origin(self, turn: TaskTurn, origin: TurnOrigin) -> TaskTurn:
        """Store the verdict and approved capabilities on a turn that proceeds."""
        updated = _with(turn, origin=origin, updated_at=utc_now())
        with self._uow():
            self._lease(updated, turn)
        return updated

    def cancel(self, turn: TaskTurn, task: Task, origin: TurnOrigin) -> TaskTurn:
        """Cancel the turn (no model, no connector) and free the task."""
        now = utc_now()
        done = _with(
            turn,
            origin=origin,
            status=TaskTurnStatus.CANCELLED,
            worker_id=None,
            finished_at=now,
            updated_at=now,
            error=None,
        )
        update = _task_with(task, now, TaskTurnStatus.CANCELLED, None)
        with self._uow():
            self._lease(done, turn)
            self._release(update, turn)
        return done

    def short_circuit(
        self, turn: TaskTurn, task: Task, origin: TurnOrigin, message: str
    ) -> TaskTurn:
        """Finish with the fixed ``message`` as the turn's only reply."""
        now = utc_now()
        reply = TaskMessage(
            message_id=new_id(),
            task_id=task.task_id,
            turn_id=turn.turn_id,
            org_id=task.org_id,
            workspace_id=task.workspace_id,
            role=TaskMessageRole.ASSISTANT,
            content=message[:32_000],
            created_by_principal_id=None,
            created_at=now,
        )
        done = _with(
            turn,
            origin=origin,
            status=TaskTurnStatus.SUCCEEDED,
            assistant_message_id=reply.message_id,
            worker_id=None,
            finished_at=now,
            updated_at=now,
        )
        update = _task_with(task, now, TaskTurnStatus.SUCCEEDED, message[:2000])
        with self._uow():
            self._messages.create(reply)
            self._lease(done, turn)
            self._release(update, turn)
        return done

    def _lease(self, new: TaskTurn, held: TaskTurn) -> None:
        if not self._turns.put_if_worker_owned(
            new, worker_id=held.worker_id or "", attempts=held.attempts
        ):
            raise RuntimeError("task turn lease was lost during the pre-run guard")

    def _release(self, update: Task, turn: TaskTurn) -> None:
        if not self._tasks.put_if_task_state(
            update,
            expected_status=TaskStatus.OPEN.value,
            expected_active_turn_id=turn.turn_id,
        ):
            raise RuntimeError("task changed during the pre-run guard")


def _with(turn: TaskTurn, **changes: object) -> TaskTurn:
    return TaskTurn.model_validate(turn.model_copy(update=changes).model_dump())


def _task_with(
    task: Task, now: object, status: TaskTurnStatus, summary: str | None
) -> Task:
    update: dict[str, object] = {
        "active_turn_id": None,
        "active_turn_status": None,
        "latest_turn_status": status,
        "latest_turn_error": None,
        "updated_at": now,
    }
    if summary is not None:
        update["latest_response_summary"] = summary
    return Task.model_validate(task.model_copy(update=update).model_dump())
