"""Queue and inspect durable evaluation executions."""

from __future__ import annotations

from datetime import datetime
from threading import RLock

from contracts.v1 import (
    AgentStatus,
    AuditEvent,
    AuditTargetScope,
    EvaluationExecution,
    EvaluationExecutionStatus,
    EvaluationSuiteRecord,
    ExecutionJob,
    ExecutionJobStatus,
    Permission,
)
from modules.evaluations.runtime_host import EvaluationRuntimeHost
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class EvaluationQueueOperationsMixin(EvaluationRuntimeHost):
    """Create, inspect, and cancel queued evaluation runs."""

    def enqueue_evaluation_run(
        self,
        suite_id: str,
        *,
        idempotency_key: str,
        requested_by_principal_id: str = "local-developer",
        _idempotency_lock_held: bool = False,
    ) -> tuple[EvaluationExecution, bool]:
        """Queue a saved suite for execution by the durable product worker."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        key = idempotency_key.strip()

        if not key or len(key) > 128:
            raise ValueError("Idempotency-Key must contain 1 to 128 characters")

        if not _idempotency_lock_held:
            lock_key = (actor, key)

            with self._idempotency_guard:
                lock = self._execution_job_idempotency_locks.setdefault(
                    lock_key, RLock()
                )

            with lock:
                return self.enqueue_evaluation_run(
                    suite_id,
                    idempotency_key=key,
                    requested_by_principal_id=actor,
                    _idempotency_lock_held=True,
                )

        self.authorize(actor, Permission.AGENTS_RUN)
        return self._enqueue_authorized(suite_id, key, actor)

    def _enqueue_authorized(
        self, suite_id: str, key: str, actor: str
    ) -> tuple[EvaluationExecution, bool]:
        """Load the suite and create or replay its durable execution."""
        suite = self.evaluation_suites.get_scoped(
            suite_id, effective_org_id(), effective_workspace_id()
        )
        agent = self.agents.get_scoped(
            suite.definition.agent_id, effective_org_id(), effective_workspace_id()
        )
        if agent.status is not AgentStatus.ACTIVE:
            raise ValueError("evaluation suites require an active agent")
        existing = self._existing_evaluation(suite_id, key, actor)
        if existing is not None:
            return existing

        timestamp = _now()
        evaluation, job = self._queued_records(suite, suite_id, key, actor, timestamp)
        try:
            self._persist_queued_execution(suite, evaluation, job, actor, timestamp)
        except IdempotencyKeyAlreadyExists:
            replay = self._existing_evaluation(suite_id, key, actor)
            if replay is None:
                raise IdempotencyConflict(
                    "Idempotency-Key was already used for a different execution request"
                ) from None
            return replay
        return evaluation, False

    def _existing_evaluation(
        self, suite_id: str, key: str, actor: str
    ) -> tuple[EvaluationExecution, bool] | None:
        """Resolve a matching idempotent request or raise on key reuse."""
        existing = self.execution_jobs.find_by_idempotency_key(
            key, actor, effective_org_id(), effective_workspace_id()
        )
        if existing is None:
            return None
        if existing.evaluation_suite_id != suite_id or existing.evaluation_id is None:
            raise IdempotencyConflict(
                "Idempotency-Key was already used for a different execution request"
            )
        execution = self.evaluation_executions.get_scoped(
            existing.evaluation_id, effective_org_id(), effective_workspace_id()
        )
        return execution, True

    @staticmethod
    def _queued_records(
        suite: EvaluationSuiteRecord,
        suite_id: str,
        key: str,
        actor: str,
        timestamp: datetime,
    ) -> tuple[EvaluationExecution, ExecutionJob]:
        """Build the initial execution and worker job records."""
        evaluation_id = _id()
        job_id = _id()
        evaluation = EvaluationExecution(
            evaluation_id=evaluation_id,
            suite_id=suite.suite_id,
            agent_id=suite.definition.agent_id,
            execution_job_id=job_id,
            requested_by_principal_id=actor,
            definition=suite.definition,
            org_id=suite.org_id,
            workspace_id=suite.workspace_id,
            status=EvaluationExecutionStatus.QUEUED,
            created_at=timestamp,
            updated_at=timestamp,
        )
        job = ExecutionJob(
            job_id=job_id,
            org_id=suite.org_id,
            workspace_id=suite.workspace_id,
            requested_by_principal_id=actor,
            task=f"Run evaluation suite {suite_id}",
            evaluation_id=evaluation_id,
            evaluation_suite_id=suite_id,
            idempotency_key=key,
            status=ExecutionJobStatus.QUEUED,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return evaluation, job

    def _persist_queued_execution(
        self,
        suite: EvaluationSuiteRecord,
        evaluation: EvaluationExecution,
        job: ExecutionJob,
        actor: str,
        timestamp: datetime,
    ) -> None:
        """Atomically persist the queued records and corresponding audit event."""
        with self._unit_of_work():
            self.evaluation_executions.create(evaluation)
            self.execution_jobs.create(job)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=suite.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=suite.workspace_id,
                    actor_principal_id=actor,
                    action="evaluation.execution.queued",
                    target_type="evaluation_execution",
                    target_id=evaluation.evaluation_id,
                    summary="Evaluation suite queued for execution",
                    request_id=job.job_id,
                    metadata={
                        "suite_id": suite.suite_id,
                        "execution_job_id": job.job_id,
                    },
                    created_at=timestamp,
                )
            )

    def list_evaluation_executions(
        self, requested_by_principal_id: str = "local-developer", limit: int = 50
    ) -> tuple[EvaluationExecution, ...]:
        """List recent executions visible to the requesting principal."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        self.authorize(actor, Permission.AGENTS_RUN)

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        records = self.evaluation_executions.list_scoped(
            effective_org_id(), effective_workspace_id()
        )

        try:
            self.authorize(actor, Permission.USERS_MANAGE)

            is_admin = True

        except PermissionError:
            is_admin = False

        visible = [
            record
            for record in records
            if is_admin or record.requested_by_principal_id == actor
        ]

        return tuple(
            sorted(visible, key=lambda item: item.created_at, reverse=True)[:limit]
        )

    def get_evaluation_execution(
        self,
        evaluation_id: str,
        requested_by_principal_id: str = "local-developer",
    ) -> EvaluationExecution:
        """Load an execution while hiding records owned by other principals."""
        if not self._is_bootstrapped():
            self.bootstrap()

        actor = effective_principal_id(requested_by_principal_id)

        self.authorize(actor, Permission.AGENTS_RUN)

        execution = self.evaluation_executions.get_scoped(
            evaluation_id, effective_org_id(), effective_workspace_id()
        )

        try:
            self.authorize(actor, Permission.USERS_MANAGE)

        except PermissionError:
            if execution.requested_by_principal_id != actor:
                raise KeyError("evaluation execution not found") from None

        return execution

    def cancel_evaluation_run(
        self,
        evaluation_id: str,
        requested_by_principal_id: str = "local-developer",
    ) -> EvaluationExecution:
        """Request cancellation and return the refreshed execution state."""
        execution = self.get_evaluation_execution(
            evaluation_id, requested_by_principal_id
        )

        self.cancel_execution_job(execution.execution_job_id, requested_by_principal_id)

        return self.get_evaluation_execution(evaluation_id, requested_by_principal_id)
