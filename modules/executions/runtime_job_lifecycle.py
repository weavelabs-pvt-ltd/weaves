"""Execution job lifecycle capability use cases."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    ConversationThreadStatus,
    EvaluationExecution,
    EvaluationExecutionStatus,
    ExecutionJob,
    ExecutionJobStatus,
    Permission,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class ExecutionJobLifecycleMixin(RuntimeCapabilityHost):
    """Own execution joblifecycle operations."""

    def list_execution_jobs(
        self, requested_by_principal_id: str = "local-developer", limit: int = 50
    ) -> tuple[ExecutionJob, ...]:
        """List visible execution jobs in newest-first order."""
        self._ensure_bootstrapped()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.AGENTS_RUN)
        jobs = self.execution_jobs.list_scoped(
            effective_org_id(), effective_workspace_id()
        )
        try:
            self.authorize(actor, Permission.USERS_MANAGE)
            is_admin = True
        except PermissionError:
            is_admin = False
        visible = [
            job for job in jobs if is_admin or job.requested_by_principal_id == actor
        ]
        return tuple(
            sorted(visible, key=lambda item: item.created_at, reverse=True)[:limit]
        )

    def get_execution_job(
        self, job_id: str, requested_by_principal_id: str = "local-developer"
    ) -> ExecutionJob:
        """Return one scoped job if the requester owns it or administers users."""
        self._ensure_bootstrapped()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.AGENTS_RUN)
        job = self.execution_jobs.get_scoped(
            job_id, effective_org_id(), effective_workspace_id()
        )
        try:
            self.authorize(actor, Permission.USERS_MANAGE)
        except PermissionError:
            if job.requested_by_principal_id != actor:
                raise KeyError("execution job not found") from None
        return job

    def cancel_execution_job(
        self, job_id: str, requested_by_principal_id: str = "local-developer"
    ) -> ExecutionJob:
        """Cancel queued work or request safe-boundary cancellation from a worker."""
        self._ensure_bootstrapped()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.AGENTS_RUN)
        job = self.execution_jobs.get_scoped(
            job_id, effective_org_id(), effective_workspace_id()
        )
        try:
            self.authorize(actor, Permission.USERS_MANAGE)
        except PermissionError:
            if job.requested_by_principal_id != actor:
                raise KeyError("execution job not found") from None
        if job.status in {
            ExecutionJobStatus.CANCEL_REQUESTED,
            ExecutionJobStatus.CANCELLED,
        }:
            return job
        if job.status not in {
            ExecutionJobStatus.QUEUED,
            ExecutionJobStatus.RUNNING,
        }:
            raise ValueError("only queued or running execution jobs can be cancelled")

        timestamp = _now()
        queued = job.status is ExecutionJobStatus.QUEUED
        target_status = (
            ExecutionJobStatus.CANCELLED
            if queued
            else ExecutionJobStatus.CANCEL_REQUESTED
        )
        updated_job = ExecutionJob.model_validate(
            job.model_copy(
                update={
                    "status": target_status,
                    "finished_at": timestamp if queued else None,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        updated = self._persist_execution_cancellation(
            job, updated_job, actor, queued, timestamp
        )
        if updated:
            return updated_job
        current = self.execution_jobs.get_scoped(
            job_id, effective_org_id(), effective_workspace_id()
        )
        if current.status in {
            ExecutionJobStatus.CANCEL_REQUESTED,
            ExecutionJobStatus.CANCELLED,
        }:
            return current
        raise ValueError("execution job was claimed or completed before cancellation")

    def _persist_execution_cancellation(
        self,
        job: ExecutionJob,
        updated_job: ExecutionJob,
        actor: str,
        queued: bool,
        timestamp: datetime,
    ) -> bool:
        """Update job/evaluation state and write its cancellation audit event."""
        with self._unit_of_work():
            updated = self._write_cancellation_status(job, updated_job, queued)
            if updated:
                if job.evaluation_id is not None:
                    self._write_evaluation_cancellation(job, queued, timestamp)
                self.audit_events.create(
                    self._execution_cancellation_audit(job, actor, queued, timestamp)
                )
        return updated

    def _write_cancellation_status(
        self, job: ExecutionJob, updated_job: ExecutionJob, queued: bool
    ) -> bool:
        """Use ownership or queued timestamp guards to claim cancellation."""
        if queued:
            return self.execution_jobs.put_if_status_and_stale(
                updated_job,
                expected_status=ExecutionJobStatus.QUEUED.value,
                updated_before=job.updated_at,
            )
        return self.execution_jobs.put_if_execution_job_owned(
            updated_job,
            worker_id=job.worker_id or "",
            attempts=job.attempts,
            expected_status=ExecutionJobStatus.RUNNING.value,
        )

    def _write_evaluation_cancellation(
        self, job: ExecutionJob, queued: bool, timestamp: datetime
    ) -> None:
        """Mirror a job cancellation state onto its queued evaluation."""
        if job.evaluation_id is None:
            return
        evaluation = self.evaluation_executions.get_scoped(
            job.evaluation_id, job.org_id, job.workspace_id
        )
        self.evaluation_executions.put(
            EvaluationExecution.model_validate(
                evaluation.model_copy(
                    update={
                        "status": (
                            EvaluationExecutionStatus.CANCELLED
                            if queued
                            else EvaluationExecutionStatus.CANCEL_REQUESTED
                        ),
                        "finished_at": timestamp if queued else None,
                        "updated_at": timestamp,
                    }
                ).model_dump()
            )
        )

    @staticmethod
    def _execution_cancellation_audit(
        job: ExecutionJob, actor: str, queued: bool, timestamp: datetime
    ) -> AuditEvent:
        """Build an audit entry for a queued cancellation or cancel request."""
        evaluation = job.evaluation_id is not None
        return AuditEvent(
            audit_event_id=_id(),
            org_id=job.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=job.workspace_id,
            actor_principal_id=actor,
            action=(
                "evaluation.execution.cancelled"
                if evaluation and queued
                else "evaluation.execution.cancel_requested"
                if evaluation
                else "agent.execution_job.cancelled"
                if queued
                else "agent.execution_job.cancel_requested"
            ),
            target_type="evaluation_execution" if evaluation else "execution_job",
            target_id=job.evaluation_id or job.job_id,
            summary=(
                "Queued execution job cancelled"
                if queued
                else "Running execution job cancellation requested"
            ),
            request_id=job.job_id,
            metadata={"requested_by_principal_id": actor},
            created_at=timestamp,
        )

    def retry_execution_job(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        requested_by_principal_id: str = "local-developer",
        _idempotency_lock_held: bool = False,
    ) -> tuple[ExecutionJob, bool]:
        """Queue a new, idempotent job linked to a retryable failed job."""
        self._ensure_bootstrapped()
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
                return self.retry_execution_job(
                    job_id,
                    idempotency_key=key,
                    requested_by_principal_id=actor,
                    _idempotency_lock_held=True,
                )
        return self._retry_execution_job_locked(job_id, actor, key)

    def _retry_execution_job_locked(
        self, job_id: str, actor: str, key: str
    ) -> tuple[ExecutionJob, bool]:
        """Validate a retry request and persist its new queued job."""
        self.authorize(actor, Permission.AGENTS_RUN)
        original = self._load_retryable_execution_job(job_id, actor)
        existing = self._find_execution_job_retry(key, actor, original.job_id)
        if existing is not None:
            return existing, True
        retry = self._build_execution_job_retry(original, actor, key)
        try:
            self._persist_execution_job_retry(original, retry, actor)
        except IdempotencyKeyAlreadyExists:
            existing = self._find_execution_job_retry(key, actor, original.job_id)
            if existing is None:
                raise
            return existing, True
        return retry, False

    def _load_retryable_execution_job(self, job_id: str, actor: str) -> ExecutionJob:
        """Load and authorize the failed job and its retry prerequisites."""
        original = self.execution_jobs.get_scoped(
            job_id, effective_org_id(), effective_workspace_id()
        )
        try:
            self.authorize(actor, Permission.USERS_MANAGE)
        except PermissionError:
            if original.requested_by_principal_id != actor:
                raise KeyError("execution job not found") from None
        if original.evaluation_id is not None:
            raise ValueError(
                "evaluation jobs cannot be retried; submit a new evaluation run"
            )
        if original.status is not ExecutionJobStatus.FAILED:
            raise ValueError("only failed execution jobs can be retried")
        if original.error is None or not original.error.retryable:
            raise ValueError("execution job failure is not marked retryable")
        if original.workflow_id is not None:
            self.authorize(actor, Permission.WORKFLOWS_RUN)
        self._ensure_retry_thread_open(original)
        return original

    def _ensure_retry_thread_open(self, original: ExecutionJob) -> None:
        """Prevent retries from targeting a closed conversation thread."""
        if original.thread_id is None:
            return
        thread = self.threads.get_scoped(
            original.thread_id, effective_org_id(), effective_workspace_id()
        )
        if thread.status is not ConversationThreadStatus.OPEN:
            raise ValueError("closed conversation threads cannot be retried")

    def _find_execution_job_retry(
        self, key: str, actor: str, original_job_id: str
    ) -> ExecutionJob | None:
        """Return the existing retry for this key or reject key reuse."""
        existing = self.execution_jobs.find_by_idempotency_key(
            key, actor, effective_org_id(), effective_workspace_id()
        )
        if existing is not None and existing.retry_of_job_id != original_job_id:
            raise IdempotencyConflict(
                "Idempotency-Key was already used for a different execution request"
            )
        return existing

    @staticmethod
    def _build_execution_job_retry(
        original: ExecutionJob, actor: str, key: str
    ) -> ExecutionJob:
        """Create a new queued record linked to its failed predecessor."""
        timestamp = _now()
        return ExecutionJob(
            job_id=_id(),
            org_id=original.org_id,
            workspace_id=original.workspace_id,
            requested_by_principal_id=actor,
            task=original.task,
            agent_id=original.agent_id,
            workflow_id=original.workflow_id,
            workflow_version_id=original.workflow_version_id,
            thread_id=original.thread_id,
            trigger_id=original.trigger_id,
            trigger_event_id=original.trigger_event_id,
            schedule_id=original.schedule_id,
            schedule_occurrence_at=original.schedule_occurrence_at,
            retry_of_job_id=original.job_id,
            idempotency_key=key,
            status=ExecutionJobStatus.QUEUED,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def _persist_execution_job_retry(
        self, original: ExecutionJob, retry: ExecutionJob, actor: str
    ) -> None:
        """Store the retry and audit record atomically."""
        with self._unit_of_work():
            self.execution_jobs.create(retry)
            self.audit_events.create(
                self._execution_job_retry_audit(original, retry, actor)
            )

    @staticmethod
    def _execution_job_retry_audit(
        original: ExecutionJob, retry: ExecutionJob, actor: str
    ) -> AuditEvent:
        """Build the audit event for an execution job retry."""
        metadata: dict[str, Any] = {"retry_of_job_id": original.job_id}
        if original.trigger_id is not None:
            metadata["trigger_id"] = original.trigger_id
        if original.schedule_id is not None:
            metadata["schedule_id"] = original.schedule_id
        return AuditEvent(
            audit_event_id=_id(),
            org_id=original.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=original.workspace_id,
            actor_principal_id=actor,
            action=(
                "workflow.execution_job.retry_queued"
                if original.workflow_id is not None
                else "agent.execution_job.retry_queued"
            ),
            target_type="execution_job",
            target_id=retry.job_id,
            summary="Retry of a failed execution job queued",
            request_id=retry.job_id,
            metadata=metadata,
            created_at=retry.created_at,
        )
