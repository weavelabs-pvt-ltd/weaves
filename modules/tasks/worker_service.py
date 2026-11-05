"""Claim queued task turns and persist their agent response or failure."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from threading import Event, Thread

from application.ports.repository import Repository
from contracts.v1 import (
    AgentRun,
    ErrorSummary,
    Task,
    TaskMessage,
    TaskMessageRole,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
    WorkflowRun,
)
from modules.agents.execution_service import AgentExecutionService
from shared.clock import new_id, utc_now
from shared.request_context import reset_request_principal, set_request_identity


class TaskWorkerService:
    """Run one durable conversation turn per worker poll iteration."""

    def __init__(
        self,
        *,
        tasks: Repository[Task],
        messages: Repository[TaskMessage],
        turns: Repository[TaskTurn],
        agent_runs: Repository[AgentRun],
        runs: Repository[WorkflowRun],
        agent_execution: AgentExecutionService,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        heartbeat_interval_seconds: float = 15,
    ) -> None:
        """Bind task storage, model execution, and transaction boundary."""
        self.tasks = tasks
        self.messages = messages
        self.turns = turns
        self.agent_runs = agent_runs
        self.runs = runs
        self.agent_execution = agent_execution
        self._open_unit_of_work = unit_of_work
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def process_next_turn(self, worker_id: str) -> TaskTurn | None:
        """Claim and process the oldest queued turn from any workspace."""
        claimed = self.turns.claim_next_queued(worker_id, utc_now(), None, None)
        if claimed is None:
            return None
        task = self.tasks.get_scoped(
            claimed.task_id, claimed.org_id, claimed.workspace_id
        )
        if task.active_turn_id != claimed.turn_id:
            raise RuntimeError("claimed task turn is not active on its task")
        task_running = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_status": TaskTurnStatus.RUNNING,
                    "latest_turn_status": TaskTurnStatus.RUNNING,
                    "updated_at": claimed.updated_at,
                }
            ).model_dump()
        )
        run_key = claimed.run_idempotency_key or (
            f"task:{claimed.turn_id}:run:{claimed.attempts}"
        )
        claimed = TaskTurn.model_validate(
            claimed.model_copy(
                update={
                    "run_idempotency_key": run_key,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            updated = self.turns.put_if_worker_owned(
                claimed,
                worker_id=worker_id,
                attempts=claimed.attempts,
            )
            if not updated:
                raise RuntimeError("task turn lease was lost before execution")
            task_changed = self.tasks.put_if_task_state(
                task_running,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=claimed.turn_id,
            )
            if not task_changed:
                raise RuntimeError("task changed before its turn started")
        user_message = self.messages.get_scoped(
            claimed.user_message_id, claimed.org_id, claimed.workspace_id
        )
        context = set_request_identity(
            claimed.requested_by_principal_id, claimed.org_id, claimed.workspace_id
        )
        heartbeat_stop = Event()
        lease_lost = Event()
        heartbeat = Thread(
            target=self._heartbeat_loop,
            args=(claimed, heartbeat_stop, lease_lost),
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self.agent_execution.run_agent(
                user_message.content,
                requested_by_principal_id=claimed.requested_by_principal_id,
                agent_id=task.agent_id,
                thread_id=task.thread_id,
                idempotency_key=run_key,
                agent_version_id=task.agent_version_id,
                _cancellation_event=lease_lost,
                _cancellation_check=lambda: lease_lost.is_set() or self.turns.get_scoped(claimed.turn_id, task.org_id, task.workspace_id).status is TaskTurnStatus.CANCELLED,
            )
            response = result.agent_run.output_summary or {}
            content = str(response.get("response", "")).strip()
            if not content:
                raise RuntimeError("agent returned an empty response")
            return self._finish_success(claimed, task, result.agent_run, content)
        except Exception as exc:
            return self._finish_failure(claimed, task, run_key, exc)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=self.heartbeat_interval_seconds + 1)
            reset_request_principal(context)

    def _heartbeat_loop(self, turn: TaskTurn, stop: Event, lease_lost: Event) -> None:
        """Renew the claimed turn lease until work ends or ownership is lost."""
        while not stop.wait(self.heartbeat_interval_seconds):
            heartbeat = TaskTurn.model_validate(
                turn.model_copy(update={"updated_at": utc_now()}).model_dump()
            )
            if not self.turns.put_if_worker_owned(
                heartbeat,
                worker_id=turn.worker_id or "",
                attempts=turn.attempts,
            ):
                lease_lost.set()
                return

    def recover_stale_turns(self, *, stale_after_seconds: int = 300) -> tuple[str, ...]:
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
                        "error": ErrorSummary(
                            code="task.worker_lease_expired",
                            summary=(
                                "The task worker stopped before completing this turn."
                            ),
                            retryable=False,
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

    def _finish_success(
        self, turn: TaskTurn, task: Task, agent_run: AgentRun, content: str
    ) -> TaskTurn:
        """Persist the assistant message and release the task's active turn."""
        now = utc_now()
        message = TaskMessage(
            message_id=new_id(),
            task_id=task.task_id,
            turn_id=turn.turn_id,
            org_id=task.org_id,
            workspace_id=task.workspace_id,
            role=TaskMessageRole.ASSISTANT,
            content=content[:32_000],
            created_by_principal_id=None,
            created_at=now,
        )
        finished = TaskTurn.model_validate(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.SUCCEEDED,
                    "assistant_message_id": message.message_id,
                    "agent_run_id": agent_run.agent_run_id,
                    "worker_id": None,
                    "finished_at": now,
                    "updated_at": now,
                }
            ).model_dump()
        )
        task_update = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_id": None,
                    "active_turn_status": None,
                    "latest_turn_status": TaskTurnStatus.SUCCEEDED,
                    "latest_response_summary": content[:2000],
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            self.messages.create(message)
            updated = self.turns.put_if_worker_owned(
                finished, worker_id=turn.worker_id or "", attempts=turn.attempts
            )
            if not updated:
                raise RuntimeError("task turn lease was lost before completion")
            task_changed = self.tasks.put_if_task_state(
                task_update,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=turn.turn_id,
            )
            if not task_changed:
                raise RuntimeError("task changed before turn completion")
        return finished

    def _finish_failure(
        self, turn: TaskTurn, task: Task, run_key: str, error: Exception
    ) -> TaskTurn:
        """Store a sanitized failure summary and allow task retry."""
        now = utc_now()
        failure = ErrorSummary(
            code="task.agent_run_failed",
            summary="The agent could not complete this task turn.",
            retryable=True,
        )
        del error
        run = self.runs.find_by_idempotency_key(
            run_key,
            turn.requested_by_principal_id,
            turn.org_id,
            turn.workspace_id,
        )
        agent_run = next(
            (
                item
                for item in self.agent_runs.list_scoped(turn.org_id, turn.workspace_id)
                if run is not None and item.run_id == run.run_id
            ),
            None,
        )
        failed = TaskTurn.model_validate(
            turn.model_copy(
                update={
                    "status": TaskTurnStatus.FAILED,
                    "worker_id": None,
                    "agent_run_id": (
                        agent_run.agent_run_id if agent_run is not None else None
                    ),
                    "finished_at": now,
                    "error": failure,
                    "updated_at": now,
                }
            ).model_dump()
        )
        task_update = Task.model_validate(
            task.model_copy(
                update={
                    "active_turn_id": None,
                    "active_turn_status": None,
                    "latest_turn_status": TaskTurnStatus.FAILED,
                    "latest_response_summary": failure.summary,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            updated = self.turns.put_if_worker_owned(
                failed, worker_id=turn.worker_id or "", attempts=turn.attempts
            )
            if not updated:
                raise RuntimeError("task turn lease was lost before failure update")
            task_changed = self.tasks.put_if_task_state(
                task_update,
                expected_status=TaskStatus.OPEN.value,
                expected_active_turn_id=turn.turn_id,
            )
            if not task_changed:
                raise RuntimeError("task changed before recording turn failure")
        return failed
