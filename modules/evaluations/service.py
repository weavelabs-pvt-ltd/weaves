"""Evaluation use cases composed with explicit persistence and execution ports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from threading import Event, RLock

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    Artifact,
    AuditEvent,
    ErrorSummary,
    EvaluationExecution,
    EvaluationReport,
    EvaluationReportRecord,
    EvaluationSuiteRecord,
    ExecutionJob,
    ExecutionJobStatus,
    Permission,
    Task,
    TaskTurn,
    ToolInvocation,
    WorkflowRun,
)
from modules.evaluations.runtime import EvaluationRuntimeMixin
from modules.workflows.runtime_result import RuntimeResult


class EvaluationService(EvaluationRuntimeMixin):
    """Run evaluation lifecycle and queue operations behind explicit ports."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_runs: Repository[AgentRun],
        artifacts: Repository[Artifact],
        audit_events: Repository[AuditEvent],
        evaluation_executions: Repository[EvaluationExecution],
        evaluation_reports: Repository[EvaluationReportRecord],
        evaluation_suites: Repository[EvaluationSuiteRecord],
        execution_jobs: Repository[ExecutionJob],
        tasks: Repository[Task],
        task_turns: Repository[TaskTurn],
        runs: Repository[WorkflowRun],
        tool_invocations: Repository[ToolInvocation],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        cancel_execution_job: Callable[[str, str], ExecutionJob],
        cancellation_requested: Callable[[str, Event], bool],
        run_agent: Callable[..., RuntimeResult],
        idempotency_guard: RLock,
        execution_job_idempotency_locks: dict[tuple[str, str], RLock],
    ) -> None:
        """Bind evaluation repositories and the execution operations it needs."""
        self.agents = agents
        self.agent_runs = agent_runs
        self.artifacts = artifacts
        self.audit_events = audit_events
        self.evaluation_executions = evaluation_executions
        self.evaluation_reports = evaluation_reports
        self.evaluation_suites = evaluation_suites
        self.execution_jobs = execution_jobs
        self.tasks = tasks
        self.task_turns = task_turns
        self.runs = runs
        self.tool_invocations = tool_invocations
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work
        self._cancel_execution_job = cancel_execution_job
        self._cancellation_requested = cancellation_requested
        self._run_agent = run_agent
        self._idempotency_guard = idempotency_guard
        self._execution_job_idempotency_locks = execution_job_idempotency_locks

    def bootstrap(self) -> None:
        """Initialize the product defaults required by evaluation operations."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Check a caller's permission through the platform authorization port."""
        self._authorize(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction supplied by the process composition root."""
        return self._open_unit_of_work()

    def cancel_execution_job(
        self, job_id: str, requested_by_principal_id: str = "local-developer"
    ) -> ExecutionJob:
        """Request cancellation through the execution capability."""
        return self._cancel_execution_job(job_id, requested_by_principal_id)

    def _execution_cancellation_requested(
        self, job_id: str, cancellation_requested: Event
    ) -> bool:
        """Read cooperative cancellation state through the execution capability."""
        return self._cancellation_requested(job_id, cancellation_requested)

    def run_agent(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
    ) -> RuntimeResult:
        """Run an evaluation case through the normal agent execution path."""
        return self._run_agent(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            idempotency_key,
            workflow_id=workflow_id,
        )

    def execute_queued_evaluation(
        self, job: ExecutionJob, cancellation_requested: Event
    ) -> EvaluationReport:
        """Execute one claimed evaluation job and return its report."""
        return self._execute_queued_evaluation(job, cancellation_requested)

    def finish_queued_evaluation(
        self,
        job: ExecutionJob,
        *,
        status: ExecutionJobStatus,
        report: EvaluationReport | None,
        error: ErrorSummary | None,
        finished_at: datetime,
    ) -> None:
        """Persist the evaluation outcome for a completed execution job."""
        self._finish_queued_evaluation(
            job,
            status=status,
            report=report,
            error=error,
            finished_at=finished_at,
        )
