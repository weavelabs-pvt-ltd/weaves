"""Execution job lifecycle capability use cases."""

from __future__ import annotations

import re
from datetime import datetime
from threading import Event

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    ErrorSummary,
    EvaluationReport,
    ExecutionJob,
    ExecutionJobStatus,
    RunStatus,
)
from modules.workflows.runtime_result import RuntimeResult as RuntimeResult
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import ExecutionCancelled


class ExecutionCompletionMixin(RuntimeCapabilityHost):
    """Own execution completion operations."""

    def _complete_claimed_execution_job(
        self, job: ExecutionJob, *, cancellation_requested: Event | None = None
    ) -> ExecutionJob:
        """Run and persist the terminal state for a previously claimed job."""
        cancellation_requested = cancellation_requested or Event()
        action, error, report, run_id, status, summary = self._execute_claimed_job(
            cancellation_requested, job
        )
        latest = self.execution_jobs.get(job.job_id)
        if latest.status not in {
            ExecutionJobStatus.RUNNING,
            ExecutionJobStatus.CANCEL_REQUESTED,
        }:
            return latest
        if latest.status is ExecutionJobStatus.CANCEL_REQUESTED and status not in {
            ExecutionJobStatus.SUCCEEDED,
            ExecutionJobStatus.FAILED,
        }:
            status, error, action, summary = self._cancelled_job_outcome(job)
        return self._persist_claimed_job_outcome(
            job,
            latest.status.value,
            action,
            error,
            report,
            run_id,
            status,
            summary,
        )

    @staticmethod
    def _cancelled_job_outcome(
        job: ExecutionJob,
    ) -> tuple[ExecutionJobStatus, None, str, str]:
        """Return cancellation details for an outcome observed after a request."""
        action = (
            "evaluation.execution.cancelled"
            if job.evaluation_id is not None
            else "agent.execution_job.cancelled"
        )
        summary = (
            "Evaluation execution cancelled between cases"
            if job.evaluation_id is not None
            else "Execution job cancelled at a safe boundary"
        )
        return ExecutionJobStatus.CANCELLED, None, action, summary

    def _persist_claimed_job_outcome(
        self,
        job: ExecutionJob,
        expected_status: str,
        action: str,
        error: ErrorSummary | None,
        report: EvaluationReport | None,
        run_id: str | None,
        status: ExecutionJobStatus,
        summary: str,
    ) -> ExecutionJob:
        """Persist the final job, evaluation, and audit records transactionally."""
        finished = _now()
        completed = self._completed_execution_job(job, status, run_id, error, finished)
        with self._unit_of_work():
            saved = self.execution_jobs.put_if_execution_job_owned(
                completed,
                worker_id=job.worker_id or "",
                attempts=job.attempts,
                expected_status=expected_status,
            )
            if not saved:
                latest = self.execution_jobs.get(job.job_id)
                if latest.status is not ExecutionJobStatus.CANCEL_REQUESTED:
                    return latest
                recovered = self._resolve_cancelled_job_completion(
                    job, latest, status, error, run_id
                )
                if recovered is None:
                    return self.execution_jobs.get(job.job_id)
                completed, status, error, action, summary, finished = recovered
            if job.evaluation_id is not None:
                self.evaluation_service.finish_queued_evaluation(
                    job,
                    status=status,
                    report=report,
                    error=error,
                    finished_at=finished,
                )
            self.audit_events.create(
                self._execution_completion_audit(job, action, summary, run_id, finished)
            )
        return completed

    @staticmethod
    def _completed_execution_job(
        job: ExecutionJob,
        status: ExecutionJobStatus,
        run_id: str | None,
        error: ErrorSummary | None,
        finished: datetime,
    ) -> ExecutionJob:
        """Build an immutable job record with terminal execution details."""
        return ExecutionJob.model_validate(
            job.model_copy(
                update={
                    "status": status,
                    "run_id": run_id,
                    "error": error,
                    "finished_at": finished,
                    "updated_at": finished,
                }
            ).model_dump()
        )

    def _resolve_cancelled_job_completion(
        self,
        job: ExecutionJob,
        latest: ExecutionJob,
        status: ExecutionJobStatus,
        error: ErrorSummary | None,
        run_id: str | None,
    ) -> (
        tuple[
            ExecutionJob,
            ExecutionJobStatus,
            ErrorSummary | None,
            str,
            str,
            datetime,
        ]
        | None
    ):
        """Resolve a cancel request that raced with a worker's terminal update."""
        finished = _now()
        if status in {ExecutionJobStatus.SUCCEEDED, ExecutionJobStatus.FAILED}:
            completed = self._completed_execution_job(
                latest, status, run_id or latest.run_id, error, finished
            )
            action, summary = self._completed_job_audit_details(job, status, error)
        else:
            status, error, action, summary = self._cancelled_job_outcome(job)
            completed = self._completed_execution_job(
                latest, status, run_id or latest.run_id, None, finished
            )
        saved = self.execution_jobs.put_if_execution_job_owned(
            completed,
            worker_id=job.worker_id or "",
            attempts=job.attempts,
            expected_status=ExecutionJobStatus.CANCEL_REQUESTED.value,
        )
        if not saved:
            return None
        action = (
            "evaluation.execution.cancelled"
            if job.evaluation_id is not None
            else "agent.execution_job.cancelled"
        )
        summary = (
            "Evaluation execution cancelled between cases"
            if job.evaluation_id is not None
            else "Execution job cancelled at a safe boundary"
        )
        return completed, status, error, action, summary, finished

    @staticmethod
    def _completed_job_audit_details(
        job: ExecutionJob, status: ExecutionJobStatus, error: ErrorSummary | None
    ) -> tuple[str, str]:
        """Choose completion audit details for a raced job update."""
        if job.evaluation_id is not None:
            return (
                "evaluation.execution.succeeded"
                if status is ExecutionJobStatus.SUCCEEDED
                else "evaluation.execution.failed",
                "Evaluation suite execution completed"
                if status is ExecutionJobStatus.SUCCEEDED
                else "Evaluation execution failed",
            )
        action = (
            "workflow.execution_job.succeeded"
            if status is ExecutionJobStatus.SUCCEEDED and job.workflow_id is not None
            else "agent.execution_job.succeeded"
            if status is ExecutionJobStatus.SUCCEEDED
            else "workflow.execution_job.failed"
            if job.workflow_id is not None
            else "agent.execution_job.failed"
        )
        summary = (
            "Workflow run job completed"
            if status is ExecutionJobStatus.SUCCEEDED and job.workflow_id is not None
            else "Agent run job completed"
            if status is ExecutionJobStatus.SUCCEEDED
            else error.summary
            if error is not None
            else "Execution failed"
        )
        return action, summary

    @staticmethod
    def _execution_completion_audit(
        job: ExecutionJob,
        action: str,
        summary: str,
        run_id: str | None,
        finished: datetime,
    ) -> AuditEvent:
        """Build an audit record for a terminal execution job."""
        evaluation = job.evaluation_id is not None
        return AuditEvent(
            audit_event_id=_id(),
            org_id=job.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=job.workspace_id,
            actor_principal_id=job.requested_by_principal_id,
            action=action,
            target_type="evaluation_execution" if evaluation else "execution_job",
            target_id=job.evaluation_id or job.job_id,
            summary=summary,
            request_id=job.job_id,
            metadata=(
                {"evaluation_id": job.evaluation_id}
                if evaluation
                else {"run_id": run_id}
                if run_id
                else {}
            ),
            created_at=finished,
        )

    def _execute_claimed_job(
        self, cancellation_requested: Event, job: ExecutionJob
    ) -> tuple[
        str,
        ErrorSummary | None,
        EvaluationReport | None,
        str | None,
        ExecutionJobStatus,
        str,
    ]:
        """Execute a claimed job and normalize success or failure metadata."""
        try:
            if cancellation_requested.is_set():
                raise ExecutionCancelled("Execution cancellation was requested")
            return self._run_claimed_job_payload(job, cancellation_requested)
        except Exception as exc:
            return self._claimed_job_failure(job, exc)

    def _run_claimed_job_payload(
        self, job: ExecutionJob, cancellation_requested: Event
    ) -> tuple[
        str,
        ErrorSummary | None,
        EvaluationReport | None,
        str | None,
        ExecutionJobStatus,
        str,
    ]:
        """Run a workflow, agent, or evaluation job and describe its outcome."""
        if job.evaluation_id is not None:
            report = self.evaluation_service.execute_queued_evaluation(
                job, cancellation_requested
            )
            return (
                "evaluation.execution.succeeded",
                None,
                report,
                None,
                ExecutionJobStatus.SUCCEEDED,
                "Evaluation suite execution completed",
            )
        result = self._run_claimed_agent_or_workflow(job, cancellation_requested)
        status = (
            ExecutionJobStatus.CANCELLED
            if result.run.status is RunStatus.CANCELLED
            else ExecutionJobStatus.SUCCEEDED
        )
        action = (
            "agent.execution_job.cancelled"
            if status is ExecutionJobStatus.CANCELLED
            else (
                "workflow.execution_job.succeeded"
                if job.workflow_id is not None
                else "agent.execution_job.succeeded"
            )
        )
        summary = (
            "Execution job cancelled at a safe boundary"
            if status is ExecutionJobStatus.CANCELLED
            else "Workflow run job completed"
            if job.workflow_id
            else "Agent run job completed"
        )
        return action, None, None, result.run.run_id, status, summary

    def _run_claimed_agent_or_workflow(
        self, job: ExecutionJob, cancellation_requested: Event
    ) -> RuntimeResult:
        """Dispatch a claimed request to its workflow or agent runner."""

        def cancellation_check() -> bool:
            return self._execution_cancellation_requested(
                job.job_id, cancellation_requested
            )

        if job.workflow_id is not None:
            return self.run_workflow(
                workflow_id=job.workflow_id,
                task=job.task,
                requested_by_principal_id=job.requested_by_principal_id,
                thread_id=job.thread_id,
                idempotency_key=job.job_id,
                pinned_workflow_version_id=job.workflow_version_id,
                _cancellation_event=cancellation_requested,
                _cancellation_check=cancellation_check,
            )
        return self.run_agent(
            task=job.task,
            requested_by_principal_id=job.requested_by_principal_id,
            agent_id=job.agent_id,
            thread_id=job.thread_id,
            idempotency_key=job.job_id,
            _cancellation_event=cancellation_requested,
            _cancellation_check=cancellation_check,
        )

    def _claimed_job_failure(
        self, job: ExecutionJob, error: Exception
    ) -> tuple[
        str,
        ErrorSummary | None,
        EvaluationReport | None,
        str | None,
        ExecutionJobStatus,
        str,
    ]:
        """Convert execution exceptions to safe job status and error summaries."""
        run = (
            None
            if job.evaluation_id is not None
            else self.runs.find_by_idempotency_key(
                job.job_id,
                job.requested_by_principal_id,
                job.org_id,
                job.workspace_id,
            )
        )
        run_id = run.run_id if run is not None else None
        code = getattr(error, "code", "execution.failed")
        if not isinstance(code, str) or not re.fullmatch(
            r"[a-zA-Z][a-zA-Z0-9_.:-]{0,127}", code
        ):
            code = "execution.failed"
        status = (
            ExecutionJobStatus.CANCELLED
            if isinstance(error, ExecutionCancelled)
            else ExecutionJobStatus.FAILED
        )
        summary = self._claimed_job_error_summary(job, run_id, status)
        error_summary = (
            None
            if status is ExecutionJobStatus.CANCELLED
            else ErrorSummary(
                code=code,
                summary=summary,
                retryable=bool(getattr(error, "retryable", False)),
            )
        )
        action = self._claimed_job_failure_action(job, status)
        return action, error_summary, None, run_id, status, summary

    @staticmethod
    def _claimed_job_error_summary(
        job: ExecutionJob,
        run_id: str | None,
        status: ExecutionJobStatus,
    ) -> str:
        """Choose a bounded, non-sensitive summary for an execution failure."""
        if job.evaluation_id is not None:
            return (
                "Evaluation execution cancelled between cases"
                if status is ExecutionJobStatus.CANCELLED
                else "Evaluation execution failed"
            )
        if status is ExecutionJobStatus.CANCELLED:
            return "Execution job cancelled before a workflow run started"
        return (
            "Execution failed; inspect the linked run for details."
            if run_id
            else (
                "Execution could not start; inspect configuration and retry "
                "with a new request."
            )
        )

    @staticmethod
    def _claimed_job_failure_action(
        job: ExecutionJob, status: ExecutionJobStatus
    ) -> str:
        """Choose the action audit name for a failed or cancelled job."""
        if job.evaluation_id is not None:
            return (
                "evaluation.execution.cancelled"
                if status is ExecutionJobStatus.CANCELLED
                else "evaluation.execution.failed"
            )
        if status is ExecutionJobStatus.CANCELLED:
            return "agent.execution_job.cancelled"
        return (
            "workflow.execution_job.failed"
            if job.workflow_id is not None
            else "agent.execution_job.failed"
        )
