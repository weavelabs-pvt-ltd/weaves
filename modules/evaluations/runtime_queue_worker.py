"""Workers that execute queued evaluation jobs and finalize their state."""

from __future__ import annotations

import hashlib
from datetime import datetime
from threading import Event

from contracts.v1 import (
    ErrorSummary,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationExecution,
    EvaluationExecutionStatus,
    EvaluationReport,
    ExecutionJob,
    ExecutionJobStatus,
)
from modules.evaluations.runtime_host import EvaluationRuntimeHost
from modules.evaluations.scoring import score_evaluation_case
from shared.clock import utc_now as _now
from shared.errors import ExecutionCancelled
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
)


class EvaluationQueueWorkerMixin(EvaluationRuntimeHost):
    """Run a claimed evaluation and persist its final report."""

    def _execute_queued_evaluation(
        self, job: ExecutionJob, cancellation_requested: Event
    ) -> EvaluationReport:

        if job.evaluation_id is None:
            raise ValueError("execution job is not an evaluation")

        evaluation = self.evaluation_executions.get_scoped(
            job.evaluation_id, job.org_id, job.workspace_id
        )

        if evaluation.status is not EvaluationExecutionStatus.QUEUED:
            raise ValueError("evaluation is not queued")

        started_at = _now()

        running = EvaluationExecution.model_validate(
            evaluation.model_copy(
                update={
                    "status": EvaluationExecutionStatus.RUNNING,
                    "started_at": started_at,
                    "updated_at": started_at,
                }
            ).model_dump()
        )

        with self._unit_of_work():
            self.evaluation_executions.put(running)

        results = list(running.results)
        case_ids = {result.case_id for result in results}
        for case in running.definition.cases:
            if case.case_id in case_ids:
                continue
            result = self._execute_evaluation_case(
                job, running, case, cancellation_requested
            )
            results.append(result)
            case_ids.add(case.case_id)
            self._persist_case_progress(job, results)

        if cancellation_requested.is_set():
            raise ExecutionCancelled("Evaluation cancellation was requested")

        passed_cases = sum(result.passed for result in results)

        return EvaluationReport(
            suite_id=running.suite_id,
            evaluation_id=running.evaluation_id,
            passed_cases=passed_cases,
            total_cases=len(results),
            pass_rate=passed_cases / len(results),
            results=tuple(results),
        )

    def _execute_evaluation_case(
        self,
        job: ExecutionJob,
        running: EvaluationExecution,
        case: EvaluationCase,
        cancellation_requested: Event,
    ) -> EvaluationCaseResult:
        """Run one suite case and score its output, sources, and model usage."""
        if self._execution_cancellation_requested(job.job_id, cancellation_requested):
            raise ExecutionCancelled("Evaluation cancellation was requested")
        result = self.run_agent(
            task=case.task,
            requested_by_principal_id=running.requested_by_principal_id,
            agent_id=running.agent_id,
            idempotency_key=(
                "evalcase_"
                + hashlib.sha256(
                    f"{running.evaluation_id}\0{case.case_id}".encode()
                ).hexdigest()
            ),
        )
        artifacts = (result.artifact, *result.workflow_artifacts)
        duration_ms = _run_duration_ms(
            result.agent_run.started_at, result.agent_run.finished_at
        )
        return score_evaluation_case(
            case,
            status=result.run.status.value,
            run_id=result.run.run_id,
            summaries=tuple(item.summary for item in artifacts),
            cited_sources=tuple(
                source.source_ref for item in artifacts for source in item.provenance
            ),
            tool_call_count=len(result.tool_invocations),
            input_tokens=result.agent_run.input_tokens,
            output_tokens=result.agent_run.output_tokens,
            estimated_cost_usd=result.agent_run.estimated_cost_usd,
            duration_ms=duration_ms,
        )

    def _persist_case_progress(
        self, job: ExecutionJob, results: list[EvaluationCaseResult]
    ) -> None:
        """Save completed cases immediately for interrupted run recovery."""
        if job.evaluation_id is None:
            return
        current = self.evaluation_executions.get_scoped(
            job.evaluation_id, job.org_id, job.workspace_id
        )
        updated = EvaluationExecution.model_validate(
            current.model_copy(
                update={"results": tuple(results), "updated_at": _now()}
            ).model_dump()
        )
        with self._unit_of_work():
            self.evaluation_executions.put(updated)

    def _finish_queued_evaluation(
        self,
        job: ExecutionJob,
        *,
        status: ExecutionJobStatus,
        report: EvaluationReport | None,
        error: ErrorSummary | None,
        finished_at: datetime,
    ) -> None:

        if job.evaluation_id is None:
            return

        current = self.evaluation_executions.get_scoped(
            job.evaluation_id, job.org_id, job.workspace_id
        )

        if status is ExecutionJobStatus.SUCCEEDED:
            if report is None:
                raise ValueError("completed evaluation has no report")

            identity_token = set_request_identity(
                current.requested_by_principal_id,
                current.org_id,
                current.workspace_id,
            )

            try:
                self.store_evaluation_report(current.definition, report)

            finally:
                reset_request_principal(identity_token)

            evaluation_status = EvaluationExecutionStatus.SUCCEEDED

        elif status is ExecutionJobStatus.CANCELLED:
            evaluation_status = EvaluationExecutionStatus.CANCELLED

        else:
            evaluation_status = EvaluationExecutionStatus.FAILED

            if error is None:
                error = ErrorSummary(
                    code="evaluation.failed",
                    summary="Evaluation execution failed.",
                    retryable=False,
                )

        updated = EvaluationExecution.model_validate(
            current.model_copy(
                update={
                    "status": evaluation_status,
                    "error": error
                    if evaluation_status is EvaluationExecutionStatus.FAILED
                    else None,
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                    **({"results": report.results} if report is not None else {}),
                }
            ).model_dump()
        )

        self.evaluation_executions.put(updated)


def _run_duration_ms(
    started_at: datetime | None, finished_at: datetime | None
) -> int | None:
    """Return elapsed run time when both agent run timestamps are available."""
    if started_at is None or finished_at is None:
        return None
    return int((finished_at - started_at).total_seconds() * 1000)
