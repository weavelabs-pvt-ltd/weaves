"""Durable execution worker loop and terminal job persistence."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Event

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    ExecutionJob,
    ExecutionJobStatus,
    WorkflowRun,
)
from modules.evaluations.service import EvaluationService
from modules.executions.runtime_completion import ExecutionCompletionMixin
from modules.executions.runtime_worker import ExecutionWorkerMixin
from modules.workflows.runtime_result import RuntimeResult
from shared.request_context import effective_org_id, effective_workspace_id


class ExecutionWorkerService(ExecutionWorkerMixin, ExecutionCompletionMixin):
    """Claim durable jobs, execute them, and persist their terminal outcomes."""

    audit_events: Repository[AuditEvent]
    execution_jobs: Repository[ExecutionJob]
    runs: Repository[WorkflowRun]

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        execution_jobs: Repository[ExecutionJob],
        runs: Repository[WorkflowRun],
        evaluation_service: EvaluationService,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        heartbeat_execution_job: Callable[..., bool],
        run_agent: Callable[..., RuntimeResult],
        run_workflow: Callable[..., RuntimeResult],
        heartbeat_interval_seconds: int,
    ) -> None:
        """Bind worker persistence and execution callbacks from app composition."""
        self.audit_events = audit_events
        self.execution_jobs = execution_jobs
        self.runs = runs
        self.evaluation_service = evaluation_service
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._open_unit_of_work = unit_of_work
        self._heartbeat_execution_job = heartbeat_execution_job
        self._run_agent = run_agent
        self._run_workflow = run_workflow
        self.EXECUTION_JOB_HEARTBEAT_SECONDS = heartbeat_interval_seconds

    def _ensure_bootstrapped(self) -> None:
        """Initialize product defaults before claiming work."""
        if not self._is_bootstrapped():
            self._bootstrap()

    def bootstrap(self) -> None:
        """Delegate built-in record initialization to the lifecycle owner."""
        self._bootstrap()

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the transaction supplied by the process composition root."""
        return self._open_unit_of_work()

    def heartbeat_execution_job(
        self, job_id: str, *, worker_id: str, attempts: int
    ) -> bool:
        """Renew the current job lease through the persistence lifecycle port."""
        return self._heartbeat_execution_job(
            job_id, worker_id=worker_id, attempts=attempts
        )

    def _execution_cancellation_requested(
        self, job_id: str, cancellation_requested: Event
    ) -> bool:
        """Refresh cooperative cancellation from the durable job record."""
        if cancellation_requested.is_set():
            return True
        try:
            latest = self.execution_jobs.get_scoped(
                job_id, effective_org_id(), effective_workspace_id()
            )
        except KeyError:
            return False
        if latest.status in {
            ExecutionJobStatus.CANCEL_REQUESTED,
            ExecutionJobStatus.CANCELLED,
        }:
            cancellation_requested.set()
        return cancellation_requested.is_set()

    def run_agent(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Run an agent through the shared agent execution capability."""
        return self._run_agent(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            idempotency_key,
            workflow_id=workflow_id,
            _cancellation_event=_cancellation_event,
            _cancellation_check=_cancellation_check,
        )

    def run_workflow(
        self,
        workflow_id: str,
        task: str,
        requested_by_principal_id: str = "local-developer",
        thread_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        pinned_workflow_version_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Run a workflow through the shared workflow execution capability."""
        return self._run_workflow(
            workflow_id=workflow_id,
            task=task,
            requested_by_principal_id=requested_by_principal_id,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            pinned_workflow_version_id=pinned_workflow_version_id,
            _cancellation_event=_cancellation_event,
            _cancellation_check=_cancellation_check,
        )
