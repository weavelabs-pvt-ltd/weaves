"""Claim queued task turns and persist their agent response or failure."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from contextvars import copy_context
from threading import Event, Thread

from application.ports.fair_claim import log_over_cap
from application.ports.limits import LimitPolicy
from application.ports.repository import Repository
from application.ports.turn_guard import TurnGuard
from application.task_run_keys import task_run_key
from contracts.v1 import (
    AgentRun,
    Task,
    TaskMessage,
    TaskMessageRole,
    TaskStatus,
    TaskTurn,
    TaskTurnStatus,
    TurnOrigin,
    WorkflowRun,
)
from modules.agents.execution_service import AgentExecutionService
from modules.tasks.guarded_turns import GuardedTurnWriter
from modules.tasks.turn_failures import is_expected_failure, turn_failure_summary
from modules.tasks.turn_recovery import TurnRecoveryMixin
from shared.clock import new_id, utc_now
from shared.correlation import bind_context, note, principal_digest
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from shared.run_origin import RunOrigin, bound_run_origin

_log = logging.getLogger(__name__)


def _run_origin(origin: TurnOrigin | None) -> RunOrigin | None:
    """The capability-narrowing view of a turn's origin (``None``: unrestricted)."""
    if origin is None:
        return None
    return RunOrigin(origin.kind, origin.capability_ids)


class TaskWorkerService(TurnRecoveryMixin):
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
        turn_guard: TurnGuard | None = None,
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
        # Set at composition (Slack): re-checks a turn's policy at pickup.
        self.turn_guard = turn_guard
        self._guarded = GuardedTurnWriter(
            tasks=tasks, messages=messages, turns=turns, unit_of_work=unit_of_work
        )

    def process_next_turn(
        self, worker_id: str, limits: LimitPolicy | None = None
    ) -> TaskTurn | None:
        """Claim and process the next queued turn from any workspace.

        With ``limits`` the claim is fair across organizations and honours each
        organization's running cap; without it the oldest queued turn wins.
        """
        with system_scope("queue"):
            if limits is None:
                claimed = self.turns.claim_next_queued(worker_id, utc_now(), None, None)
            else:
                fair = self.turns.claim_next_fair(worker_id, utc_now(), limits)
                claimed = None if fair is None else fair.record
                if fair is not None:
                    log_over_cap("task turn", fair.record.turn_id, fair)
        if claimed is None:
            return None
        with bind_context(
            request_id=claimed.request_id,
            org_id=claimed.org_id,
            workspace_id=claimed.workspace_id,
            principal=principal_digest(claimed.requested_by_principal_id),
            turn_id=claimed.turn_id,
            worker_id=worker_id,
        ):
            return self._run_claimed_turn(claimed, worker_id)

    def _run_claimed_turn(self, claimed: TaskTurn, worker_id: str) -> TaskTurn:
        """Run a claimed turn under its tenant identity and correlation scope."""
        context = set_request_identity(
            claimed.requested_by_principal_id, claimed.org_id, claimed.workspace_id
        )
        try:
            finished = self._process_claimed_turn(claimed, worker_id)
            note(run_id=finished.agent_run_id)
            _log.info(
                "task turn finished",
                extra={"event": "turn_finished", "outcome": finished.status.value},
            )
            return finished
        finally:
            reset_request_principal(context)

    def _process_claimed_turn(self, claimed: TaskTurn, worker_id: str) -> TaskTurn:
        """Run one claimed turn under the claiming tenant's own scope."""
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
                    "latest_turn_error": None,
                    "updated_at": claimed.updated_at,
                }
            ).model_dump()
        )
        run_key = claimed.run_idempotency_key or task_run_key(
            claimed.turn_id, claimed.attempts
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
        if claimed.origin is not None:
            # Before the heartbeat starts: it rewrites the turn, and the guard's
            # verdict and approved capabilities must not be overwritten by it.
            try:
                finished, claimed = self._pre_run_guard(
                    claimed, task, user_message.content, run_key
                )
            except Exception as exc:
                return self._finish_failure(claimed, task, run_key, exc)
            if finished is not None:
                return finished
        heartbeat_stop = Event()
        lease_lost = Event()
        # The lease renewal must run as this tenant: a bare thread starts with an
        # empty context, so under row-level security its update matched no row,
        # the lease looked lost and any turn longer than the heartbeat failed.
        heartbeat = Thread(
            target=copy_context().run,
            args=(self._heartbeat_loop, claimed, heartbeat_stop, lease_lost),
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._execute_turn(
                claimed, task, user_message.content, run_key, lease_lost
            )
        except Exception as exc:
            return self._finish_failure(claimed, task, run_key, exc)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=self.heartbeat_interval_seconds + 1)

    def _pre_run_guard(
        self, turn: TaskTurn, task: Task, question: str, run_key: str
    ) -> tuple[TaskTurn | None, TaskTurn]:
        """Re-check a Slack turn's policy at pickup (design 3.5.2).

        Returns ``(finished turn, turn)`` when the turn ended here (cancelled
        ``slack_access_revoked`` or answered with a fixed message), else
        ``(None, turn with the approved capabilities and the verdict)``. A guarded
        turn with no guard configured keeps no approved list and runs no connector.
        """
        decision = (
            self.turn_guard.check(turn, task, question, run_key)
            if self.turn_guard is not None
            else None
        )
        if decision is None:
            return None, turn
        if decision.proceed:
            return None, self._guarded.record_origin(turn, decision.origin)
        if decision.cancel_reason is not None:
            return self._guarded.cancel(turn, task, decision.origin), turn
        done = self._guarded.short_circuit(
            turn, task, decision.origin, decision.message or ""
        )
        return done, turn

    def _execute_turn(
        self,
        turn: TaskTurn,
        task: Task,
        message: str,
        run_key: str,
        lease_lost: Event,
    ) -> TaskTurn:
        """Run the agent with private snapshots and finish a successful turn."""
        with bound_run_origin(_run_origin(turn.origin)):
            result = self.agent_execution.run_agent(
                message,
                requested_by_principal_id=turn.requested_by_principal_id,
                agent_id=task.agent_id,
                thread_id=task.thread_id,
                idempotency_key=run_key,
                agent_version_id=task.agent_version_id,
                _runtime_skill_snapshots=task.private_skill_snapshots,
                _cancellation_event=lease_lost,
                _cancellation_check=lambda: self._cancellation_requested(
                    turn, task, lease_lost
                ),
            )
        response = result.agent_run.output_summary or {}
        content = str(response.get("response", "")).strip()
        if not content:
            raise RuntimeError("agent returned an empty response")
        return self._finish_success(turn, task, result.agent_run, content)

    def _cancellation_requested(
        self, turn: TaskTurn, task: Task, lease_lost: Event
    ) -> bool:
        """Check local lease loss and durable cancellation state."""
        return lease_lost.is_set() or (
            self.turns.get_scoped(turn.turn_id, task.org_id, task.workspace_id).status
            is TaskTurnStatus.CANCELLED
        )

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
                    "latest_turn_error": None,
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
        failure = turn_failure_summary(error)
        # The owner sees only the user-safe summary. The operator log keeps the
        # cause, with a stack trace for unexpected failures.
        _log.warning(
            "task turn failed",
            exc_info=None if is_expected_failure(failure) else error,
            extra={
                "event": "turn_failed",
                "failure_code": failure.code,
                "error_type": type(error).__name__,
            },
        )
        run = self.runs.find_by_idempotency_key(
            run_key,
            turn.requested_by_principal_id,
            turn.org_id,
            turn.workspace_id,
        )
        agent_run = next(
            iter(
                self.agent_runs.list_by_field_in(
                    "run_id",
                    (run.run_id,),
                    org_id=turn.org_id,
                    workspace_id=turn.workspace_id,
                )
                if run is not None
                else ()
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
                    "latest_turn_error": failure,
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
