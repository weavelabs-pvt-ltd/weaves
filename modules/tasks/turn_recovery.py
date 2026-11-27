"""Requeue or fail task turns whose worker lease expired."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta

from application.ports.repository import Repository
from application.runtime_constants import STALE_TASK_TURN_AFTER_SECONDS
from contracts.v1 import ErrorSummary, Task, TaskStatus, TaskTurn, TaskTurnStatus
from shared.clock import utc_now
from shared.request_context import system_scoped


class TurnRecoveryMixin:
    """Recover stale task turns for the task worker."""

    tasks: Repository[Task]
    turns: Repository[TaskTurn]
    _open_unit_of_work: Callable[[], AbstractContextManager[None]]

    @system_scoped("recovery")
    def recover_stale_turns(
        self, *, stale_after_seconds: int = STALE_TASK_TURN_AFTER_SECONDS
    ) -> tuple[str, ...]:
        """Requeue expired worker leases or fail after three attempts."""
        now = utc_now()
        stale = self.turns.list_status_updated_before(
            TaskTurnStatus.RUNNING.value,
            now - timedelta(seconds=stale_after_seconds),
            limit=100,
        )
        recovered: list[str] = []
        for turn in stale:
            if turn.attempts >= 3:
                replacement = turn.model_copy(
                    update={
                        "status": TaskTurnStatus.FAILED,
                        "worker_id": None,
                        "finished_at": now,
                        # Automatic attempts are used up, but the failure was the
                        # worker's, not the turn's: a person may retry it.
                        "error": ErrorSummary(
                            code="task.worker_lease_expired",
                            summary=(
                                "The task worker stopped before completing this turn."
                            ),
                            retryable=True,
                        ),
                        "updated_at": now,
                    }
                )
                task = self.tasks.get_scoped(
                    turn.task_id, turn.org_id, turn.workspace_id
                )
                if task.active_turn_id != turn.turn_id:
                    continue
                task_update = task.model_copy(
                    update={
                        "active_turn_id": None,
                        "active_turn_status": None,
                        "latest_turn_status": TaskTurnStatus.FAILED,
                        "latest_turn_error": replacement.error,
                        "latest_response_summary": (
                            "Task turn failed after worker recovery."
                        ),
                        "updated_at": now,
                    }
                )
            else:
                replacement = turn.model_copy(
                    update={
                        "status": TaskTurnStatus.QUEUED,
                        "worker_id": None,
                        "started_at": None,
                        "finished_at": None,
                        "error": None,
                        "updated_at": now,
                    }
                )
                task = self.tasks.get_scoped(
                    turn.task_id, turn.org_id, turn.workspace_id
                )
                if task.active_turn_id != turn.turn_id:
                    continue
                task_update = task.model_copy(
                    update={
                        "active_turn_status": TaskTurnStatus.QUEUED,
                        "latest_turn_status": TaskTurnStatus.QUEUED,
                        "latest_turn_error": None,
                        "updated_at": now,
                    }
                )
            if self._persist_turn_recovery(
                turn, replacement, task_update, now, stale_after_seconds
            ):
                recovered.append(turn.turn_id)
        return tuple(recovered)

    def _persist_turn_recovery(
        self,
        turn: TaskTurn,
        replacement: TaskTurn,
        task_update: Task,
        now: datetime,
        stale_after_seconds: int,
    ) -> bool:
        """Atomically update a stale turn lease and its owning task state."""
        with self._open_unit_of_work():
            changed = self.turns.put_if_status_and_stale(
                replacement,
                expected_status=TaskTurnStatus.RUNNING.value,
                updated_before=now - timedelta(seconds=stale_after_seconds),
            )
            if not changed:
                return False
            task_changed = self.tasks.put_if_task_state(
                Task.model_validate(task_update.model_dump()),
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=turn.turn_id,
            )
            if not task_changed:
                raise RuntimeError("task changed during turn recovery")
            return True
