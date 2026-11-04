"""Typed dependencies shared by workflow definition use cases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentVersion,
    AuditEvent,
    Permission,
    WorkflowDefinition,
    WorkflowVersion,
)


class WorkflowDefinitionRuntimeHost(ABC):
    """Dependencies needed to create and publish workflow definitions."""

    _is_bootstrapped: Callable[[], bool]
    agent_versions: Repository[AgentVersion]
    agents: Repository[AgentDefinition]
    audit_events: Repository[AuditEvent]
    workflow_versions: Repository[WorkflowVersion]
    workflows: Repository[WorkflowDefinition]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before workflow operations."""
        raise NotImplementedError

    @abstractmethod
    def _ensure_workflow_runtime(self) -> None:
        """Initialize the application before a workflow definition operation."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workflow permissions for the active request scope."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for versioned workflow writes."""
        raise NotImplementedError
