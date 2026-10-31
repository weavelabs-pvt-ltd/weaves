"""Shared platform boundary for workflow execution capabilities."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Event, RLock
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentVersion,
    AuditEvent,
    ConversationThread,
    ModelProfile,
    Permission,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.workflows.runtime_result import RuntimeResult


class WorkflowExecutionRuntimeHost(ABC):
    """Expose runtime-owned dependencies to workflow execution use cases."""

    _bootstrapped: Callable[[], bool]
    _idempotency_guard: RLock
    _idempotency_locks: dict[tuple[str, str], RLock]
    runs: Repository[WorkflowRun]
    workflows: Repository[WorkflowDefinition]
    workflow_versions: Repository[WorkflowVersion]
    agent_versions: Repository[AgentVersion]
    agents: Repository[AgentDefinition]
    model_profiles: Repository[ModelProfile]
    providers: Repository[ModelProviderContract]
    threads: Repository[ConversationThread]
    audit_events: Repository[AuditEvent]

    def bootstrap(self) -> None:
        """Initialize runtime defaults before execution."""
        raise NotImplementedError

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce the active principal's permission."""
        raise NotImplementedError

    def _execute_workflow_sequence(
        self,
        workflow_id: str,
        task: str,
        actor: str,
        thread_id: str | None,
        idempotency_key: str | None,
        *,
        pinned_workflow_version_id: str | None = None,
        cancellation_event: Event | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Execute an immutable workflow version."""
        raise NotImplementedError

    def _replay_idempotent_run(
        self,
        run: Any,
        *,
        task: str,
        agent_id: str | None,
        thread_id: str | None,
        workflow_id: str | None,
    ) -> RuntimeResult:
        """Return a prior run after validating request equivalence."""
        raise NotImplementedError

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the persistence transaction used by workflow updates."""
        raise NotImplementedError

    def _execute_agent_run(self, *args: Any, **kwargs: Any) -> RuntimeResult:
        """Execute one agent step from a workflow."""
        raise NotImplementedError
