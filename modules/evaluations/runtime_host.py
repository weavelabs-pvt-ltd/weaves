"""Typed host contract shared by evaluation runtime capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Event, RLock
from typing import TYPE_CHECKING

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    Artifact,
    AuditEvent,
    EvaluationExecution,
    EvaluationReport,
    EvaluationReportRecord,
    EvaluationSuite,
    EvaluationSuiteRecord,
    ExecutionJob,
    Permission,
    ToolInvocation,
    WorkflowRun,
)

if TYPE_CHECKING:
    from modules.workflows.runtime_result import RuntimeResult


class EvaluationRuntimeHost(ABC):
    """Typed runtime capabilities required by evaluation operations."""

    _is_bootstrapped: Callable[[], bool]
    _idempotency_guard: RLock
    _execution_job_idempotency_locks: dict[tuple[str, str], RLock]
    agents: Repository[AgentDefinition]
    artifacts: Repository[Artifact]
    agent_runs: Repository[AgentRun]
    audit_events: Repository[AuditEvent]
    evaluation_executions: Repository[EvaluationExecution]
    evaluation_reports: Repository[EvaluationReportRecord]
    evaluation_suites: Repository[EvaluationSuiteRecord]
    execution_jobs: Repository[ExecutionJob]
    runs: Repository[WorkflowRun]
    tool_invocations: Repository[ToolInvocation]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize the runtime before accessing durable state."""
        raise NotImplementedError

    @abstractmethod
    def store_evaluation_report(
        self,
        definition: EvaluationSuite,
        report: EvaluationReport,
        requested_by_principal_id: str = "local-developer",
    ) -> EvaluationReportRecord:
        """Persist a validated report generated from completed run evidence."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Verify that the principal may perform an operation."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Return an atomic persistence scope."""
        raise NotImplementedError

    @abstractmethod
    def cancel_execution_job(
        self, job_id: str, requested_by_principal_id: str = "local-developer"
    ) -> ExecutionJob:
        """Request cancellation of a durable execution job."""
        raise NotImplementedError

    @abstractmethod
    def _execution_cancellation_requested(
        self, job_id: str, cancellation_requested: Event
    ) -> bool:
        """Check both persisted and in-process cancellation signals."""
        raise NotImplementedError

    @abstractmethod
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
        """Run an agent task through the configured runtime."""
        raise NotImplementedError
