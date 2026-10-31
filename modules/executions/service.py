"""Execution queue and job lifecycle use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import RLock

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentVersion,
    AuditEvent,
    ConversationThread,
    EvaluationExecution,
    ExecutionJob,
    Permission,
    WorkflowDefinition,
    WorkflowVersion,
)
from modules.executions.runtime_job_lifecycle import ExecutionJobLifecycleMixin
from modules.executions.runtime_queue import ExecutionQueueMixin


class ExecutionService(ExecutionJobLifecycleMixin, ExecutionQueueMixin):
    """Own durable job submission, retry, cancellation, and job queries."""

    agents: Repository[AgentDefinition]
    agent_versions: Repository[AgentVersion]
    audit_events: Repository[AuditEvent]
    evaluation_executions: Repository[EvaluationExecution]
    execution_jobs: Repository[ExecutionJob]
    threads: Repository[ConversationThread]
    workflows: Repository[WorkflowDefinition]
    workflow_versions: Repository[WorkflowVersion]

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_versions: Repository[AgentVersion],
        audit_events: Repository[AuditEvent],
        evaluation_executions: Repository[EvaluationExecution],
        execution_jobs: Repository[ExecutionJob],
        threads: Repository[ConversationThread],
        workflows: Repository[WorkflowDefinition],
        workflow_versions: Repository[WorkflowVersion],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        default_agent_id: Callable[[], str],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        idempotency_guard: RLock,
        execution_job_idempotency_locks: dict[tuple[str, str], RLock],
    ) -> None:
        """Bind repositories and platform lifecycle ports for job operations."""
        self.agents = agents
        self.agent_versions = agent_versions
        self.audit_events = audit_events
        self.evaluation_executions = evaluation_executions
        self.execution_jobs = execution_jobs
        self.threads = threads
        self.workflows = workflows
        self.workflow_versions = workflow_versions
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._resolve_default_agent_id = default_agent_id
        self._open_unit_of_work = unit_of_work
        self._idempotency_guard = idempotency_guard
        self._execution_job_idempotency_locks = execution_job_idempotency_locks

    def _ensure_bootstrapped(self) -> None:
        """Initialize product defaults before running a job use case."""
        if not self._is_bootstrapped():
            self._bootstrap()

    def bootstrap(self) -> None:
        """Delegate default record creation to the lifecycle owner."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Check permissions using the active identity capability."""
        self._authorize(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction from the persistence composition root."""
        return self._open_unit_of_work()

    def _default_agent_id(self) -> str:
        """Resolve the configured default agent through the platform boundary."""
        return self._resolve_default_agent_id()

    @staticmethod
    def _validate_workflow_handler(workflow_id: str, version: WorkflowVersion) -> None:
        """Validate that a published workflow uses a supported execution handler."""
        if version.workflow_id != workflow_id:
            raise ValueError("workflow version does not belong to this workflow")
        count = len(version.agent_version_ids)
        if version.handler_key == "agent.execute" and count != 1:
            raise ValueError("agent.execute workflows must reference one agent version")
        if version.handler_key == "agent.sequence" and not 2 <= count <= 10:
            raise ValueError(
                "agent.sequence workflows require two to ten agent versions"
            )
        if version.handler_key not in {"agent.execute", "agent.sequence"}:
            raise ValueError("workflow handler is not supported by this runtime")
