"""Execute published workflows through explicit persistence and run ports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import RLock
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentVersion,
    AuditEvent,
    ConversationThread,
    ModelProfile,
    ModelProvider,
    Permission,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)
from modules.workflows.runtime_execution import WorkflowExecutionMixin
from modules.workflows.runtime_result import RuntimeResult


class WorkflowExecutionService(WorkflowExecutionMixin):
    """Run immutable workflow versions with injected agent execution ports."""

    runs: Repository[WorkflowRun]
    workflows: Repository[WorkflowDefinition]
    workflow_versions: Repository[WorkflowVersion]
    agent_versions: Repository[AgentVersion]
    agents: Repository[AgentDefinition]
    model_profiles: Repository[ModelProfile]
    providers: Repository[ModelProvider]
    threads: Repository[ConversationThread]
    audit_events: Repository[AuditEvent]

    def __init__(
        self,
        *,
        runs: Repository[WorkflowRun],
        workflows: Repository[WorkflowDefinition],
        workflow_versions: Repository[WorkflowVersion],
        agent_versions: Repository[AgentVersion],
        agents: Repository[AgentDefinition],
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProvider],
        threads: Repository[ConversationThread],
        audit_events: Repository[AuditEvent],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        idempotency_guard: RLock,
        idempotency_locks: dict[tuple[str, str], RLock],
        execute_agent_run: Callable[..., RuntimeResult],
        replay_idempotent_run: Callable[..., RuntimeResult],
    ) -> None:
        """Bind workflow repositories, policies, and agent execution callbacks."""
        self.runs = runs
        self.workflows = workflows
        self.workflow_versions = workflow_versions
        self.agent_versions = agent_versions
        self.agents = agents
        self.model_profiles = model_profiles
        self.providers = providers
        self.threads = threads
        self.audit_events = audit_events
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work
        self._idempotency_guard = idempotency_guard
        self._idempotency_locks = idempotency_locks
        self._execute_agent_run_impl = execute_agent_run
        self._replay_idempotent_run_impl = replay_idempotent_run

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workflow permissions through the identity capability."""
        self._authorize(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for workflow run persistence."""
        return self._open_unit_of_work()

    def _execute_agent_run(self, *args: Any, **kwargs: Any) -> RuntimeResult:
        """Execute one agent step using the platform's agent run pipeline."""
        return self._execute_agent_run_impl(*args, **kwargs)

    def _replay_idempotent_run(self, *args: Any, **kwargs: Any) -> RuntimeResult:
        """Validate and replay a prior run through the shared run policy."""
        return self._replay_idempotent_run_impl(*args, **kwargs)
