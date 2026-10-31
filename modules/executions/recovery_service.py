"""Recover interrupted workflow, execution, and action state."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any

from application.ports.repository import Repository
from application.runtime_constants import (
    STALE_EXECUTION_JOB_AFTER_SECONDS,
    STALE_RUN_AFTER_SECONDS,
)
from contracts.v1 import (
    AgentRun,
    AuditEvent,
    AuditTargetScope,
    ErrorSummary,
    EvaluationExecution,
    EvaluationReport,
    ExecutionJob,
    ExecutionJobStatus,
    RunStatus,
    WorkflowRun,
)
from modules.evaluations.service import EvaluationService
from shared.clock import utc_now as _now


class ExecutionRecoveryService:
    """Recover stale agent runs, queued jobs, and approved actions."""

    def __init__(
        self,
        *,
        runs: Repository[WorkflowRun],
        agent_runs: Repository[AgentRun],
        audit_events: Repository[AuditEvent],
        execution_jobs: Repository[ExecutionJob],
        evaluation_executions: Repository[EvaluationExecution],
        evaluation_service: EvaluationService,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind recovery repositories and cross-capability use cases."""
        self.runs = runs
        self.agent_runs = agent_runs
        self.audit_events = audit_events
        self.execution_jobs = execution_jobs
        self.evaluation_executions = evaluation_executions
        self.evaluation_service = evaluation_service
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._open_unit_of_work = unit_of_work

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the configured transaction for a recovery write."""
        return self._open_unit_of_work()

    def recover_stale_runs(
        self,
        *,
        stale_after_seconds: int = STALE_RUN_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Fail runs with no execution heartbeat beyond the bounded call window."""
        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be positive")
        timestamp = now or _now()
        stale_before = timestamp - timedelta(seconds=stale_after_seconds)
        recovered: list[str] = []
        for run in self.runs.list():
            newly_recovered = False
            with self._unit_of_work():
                error = run.error
                if run.status is RunStatus.RUNNING and run.updated_at <= stale_before:
                    error = ErrorSummary(
                        code="runtime.interrupted",
                        summary=(
                            "Run was marked failed after its execution "
                            "heartbeat became stale."
                        ),
                        retryable=True,
                    )
                    failed_run = WorkflowRun.model_validate(
                        run.model_copy(
                            update={
                                "status": RunStatus.FAILED,
                                "error": error,
                                "finished_at": timestamp,
                                "updated_at": timestamp,
                            }
                        ).model_dump()
                    )
                    if not self.runs.put_if_status_and_stale(
                        failed_run,
                        expected_status=RunStatus.RUNNING.value,
                        updated_before=stale_before,
                    ):
                        continue
                    newly_recovered = True
                elif not (
                    run.status is RunStatus.FAILED
                    and error is not None
                    and error.code == "runtime.interrupted"
                ):
                    continue
                if error is None:
                    raise RuntimeError("recovered run is missing its failure summary")
                for agent_run in self.agent_runs.list_scoped(
                    run.org_id, run.workspace_id
                ):
                    if (
                        agent_run.run_id != run.run_id
                        or agent_run.status is not RunStatus.RUNNING
                    ):
                        continue
                    failed_agent_run = AgentRun.model_validate(
                        agent_run.model_copy(
                            update={
                                "status": RunStatus.FAILED,
                                "error": error,
                                "finished_at": timestamp,
                                "updated_at": timestamp,
                            }
                        ).model_dump()
                    )
                    self.agent_runs.put(failed_agent_run)
                self._ensure_run_recovery_audit(run, error, timestamp)
            if newly_recovered:
                recovered.append(run.run_id)
        return tuple(recovered)

    def _ensure_run_recovery_audit(
        self, run: WorkflowRun, error: ErrorSummary, timestamp: datetime
    ) -> None:
        existing = any(
            event.target_id == run.run_id and event.action == "agent.run.recovered"
            for event in self.audit_events.list_scoped(run.org_id, run.workspace_id)
        )
        if existing:
            return
        audit_event = AuditEvent(
            audit_event_id=(
                "recovery-" + hashlib.sha256(run.run_id.encode()).hexdigest()[:32]
            ),
            org_id=run.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=run.workspace_id,
            actor_principal_id=run.requested_by_principal_id,
            action="agent.run.recovered",
            target_type="workflow_run",
            target_id=run.run_id,
            summary=error.summary,
            request_id=run.run_id,
            metadata={"error_code": error.code},
            created_at=timestamp,
        )
        self.audit_events.put(audit_event)

    def recover_stale_execution_jobs(
        self,
        *,
        stale_after_seconds: int = STALE_EXECUTION_JOB_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Reconcile jobs left running by a terminated worker process."""
        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be positive")
        timestamp = now or _now()
        stale_before = timestamp - timedelta(seconds=stale_after_seconds)
        recovered: list[str] = []
        self._recover_stale_execution_job_records(recovered, stale_before, timestamp)
        return tuple(recovered)

    def _recover_stale_execution_job_records(
        self, recovered: list[str], stale_before: datetime, timestamp: datetime
    ) -> None:
        """Fail execution jobs whose heartbeat is stale."""
        for job in self.execution_jobs.list():
            if (
                job.status
                not in {
                    ExecutionJobStatus.RUNNING,
                    ExecutionJobStatus.CANCEL_REQUESTED,
                }
                or job.updated_at > stale_before
            ):
                continue
            run, report, status, error, summary, action = (
                self._resolve_stale_execution_job(job)
            )
            repaired = ExecutionJob.model_validate(
                job.model_copy(
                    update={
                        "status": status,
                        "run_id": run.run_id if run is not None else None,
                        "error": error,
                        "finished_at": timestamp,
                        "updated_at": timestamp,
                    }
                ).model_dump()
            )
            with self._unit_of_work():
                if not self.execution_jobs.put_if_status_and_stale(
                    repaired,
                    expected_status=job.status.value,
                    updated_before=stale_before,
                ):
                    continue
                if job.evaluation_id is not None:
                    self.evaluation_service.finish_queued_evaluation(
                        job,
                        status=status,
                        report=report,
                        error=error,
                        finished_at=timestamp,
                    )
                self.audit_events.put(
                    AuditEvent(
                        audit_event_id=(
                            "recovery-"
                            + hashlib.sha256(job.job_id.encode()).hexdigest()[:32]
                        ),
                        org_id=job.org_id,
                        target_scope=AuditTargetScope.WORKSPACE,
                        workspace_id=job.workspace_id,
                        actor_principal_id=job.requested_by_principal_id,
                        action=action,
                        target_type=(
                            "evaluation_execution"
                            if job.evaluation_id is not None
                            else "execution_job"
                        ),
                        target_id=job.evaluation_id or job.job_id,
                        summary=summary,
                        request_id=job.job_id,
                        metadata=(
                            {"evaluation_id": job.evaluation_id}
                            if job.evaluation_id is not None
                            else {"run_id": run.run_id}
                            if run is not None
                            else {}
                        ),
                        created_at=timestamp,
                    )
                )
            recovered.append(job.job_id)

    def _resolve_stale_execution_job(
        self, job: ExecutionJob
    ) -> tuple[
        WorkflowRun | None,
        EvaluationReport | None,
        ExecutionJobStatus,
        ErrorSummary | None,
        str,
        str,
    ]:
        """Determine the recoverable terminal outcome for an expired lease."""
        run = None
        if job.evaluation_id is None:
            run = self.runs.find_by_idempotency_key(
                job.job_id,
                job.requested_by_principal_id,
                job.org_id,
                job.workspace_id,
            )
        if job.evaluation_id is not None:
            return self._resolve_stale_evaluation_job(job)
        if job.status is ExecutionJobStatus.CANCEL_REQUESTED:
            return (
                run,
                None,
                ExecutionJobStatus.CANCELLED,
                None,
                "Cancellation completed after the worker lease expired",
                "agent.execution_job.cancelled_after_interruption",
            )
        if run is not None and run.status is RunStatus.SUCCEEDED:
            return (
                run,
                None,
                ExecutionJobStatus.SUCCEEDED,
                None,
                "Recovered completion for an interrupted worker",
                "agent.execution_job.recovered_succeeded",
            )
        error = (
            run.error
            if run is not None and run.error is not None
            else ErrorSummary(
                code="runtime.interrupted",
                summary="Execution worker stopped before the job completed.",
                retryable=True,
            )
        )
        return (
            run,
            None,
            ExecutionJobStatus.FAILED,
            error,
            error.summary,
            "agent.execution_job.recovered_failed",
        )

    def _resolve_stale_evaluation_job(
        self, job: ExecutionJob
    ) -> tuple[None, EvaluationReport | None, ExecutionJobStatus, Any, str, str]:
        """Recover completion, cancellation, or failure for an evaluation job."""
        evaluation = self.evaluation_executions.get_scoped(
            job.evaluation_id or "", job.org_id, job.workspace_id
        )
        if job.status is ExecutionJobStatus.CANCEL_REQUESTED:
            return (
                None,
                None,
                ExecutionJobStatus.CANCELLED,
                None,
                "Evaluation cancelled after the worker lease expired",
                "evaluation.execution.cancelled_after_interruption",
            )
        result_ids = {result.case_id for result in evaluation.results}
        case_ids = {case.case_id for case in evaluation.definition.cases}
        if result_ids == case_ids:
            report = EvaluationReport(
                suite_id=evaluation.suite_id,
                evaluation_id=evaluation.evaluation_id,
                passed_cases=sum(result.passed for result in evaluation.results),
                total_cases=len(evaluation.results),
                pass_rate=(
                    sum(result.passed for result in evaluation.results)
                    / len(evaluation.results)
                ),
                results=evaluation.results,
            )
            return (
                None,
                report,
                ExecutionJobStatus.SUCCEEDED,
                None,
                "Recovered completion for an interrupted evaluation",
                "evaluation.execution.recovered_succeeded",
            )
        error = ErrorSummary(
            code="runtime.interrupted",
            summary="Evaluation worker stopped before all cases completed.",
            retryable=False,
        )
        return (
            None,
            None,
            ExecutionJobStatus.FAILED,
            error,
            error.summary,
            "evaluation.execution.recovered_failed",
        )

    def heartbeat_execution_job(
        self, job_id: str, *, worker_id: str, attempts: int
    ) -> bool:
        """Renew a job lease only while its owner and fencing attempt match."""
        if not self._is_bootstrapped():
            self._bootstrap()
        try:
            current = self.execution_jobs.get(job_id)
        except KeyError:
            return False
        if (
            current.status
            not in {
                ExecutionJobStatus.RUNNING,
                ExecutionJobStatus.CANCEL_REQUESTED,
            }
            or current.worker_id != worker_id
            or current.attempts != attempts
        ):
            return False
        refreshed = ExecutionJob.model_validate(
            current.model_copy(update={"updated_at": _now()}).model_dump()
        )
        with self._unit_of_work():
            return self.execution_jobs.put_if_execution_job_owned(
                refreshed,
                worker_id=worker_id,
                attempts=attempts,
                expected_status=current.status.value,
            )
