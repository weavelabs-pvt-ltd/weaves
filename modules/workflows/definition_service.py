"""Workflow definition creation, versioning, and query use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentVersion,
    AuditEvent,
    Permission,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)
from modules.workflows.runtime_creation import WorkflowCreationMixin
from modules.workflows.runtime_queries import WorkflowQueryMixin
from modules.workflows.runtime_replacement import WorkflowReplacementMixin
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowDefinitionService(
    WorkflowCreationMixin, WorkflowQueryMixin, WorkflowReplacementMixin
):
    """Manage workflow definitions through repositories and identity ports."""

    agent_versions: Repository[AgentVersion]
    agents: Repository[AgentDefinition]
    audit_events: Repository[AuditEvent]
    runs: Repository[WorkflowRun]
    workflow_versions: Repository[WorkflowVersion]
    workflows: Repository[WorkflowDefinition]

    def __init__(
        self,
        *,
        agent_versions: Repository[AgentVersion],
        agents: Repository[AgentDefinition],
        audit_events: Repository[AuditEvent],
        runs: Repository[WorkflowRun],
        workflow_versions: Repository[WorkflowVersion],
        workflows: Repository[WorkflowDefinition],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind workflow repositories and application lifecycle callbacks."""
        self.agent_versions = agent_versions
        self.agents = agents
        self.audit_events = audit_events
        self.runs = runs
        self.workflow_versions = workflow_versions
        self.workflows = workflows
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work

    def _ensure_workflow_runtime(self) -> None:
        """Bootstrap product defaults before workflow definition operations."""
        if not self._is_bootstrapped():
            self.bootstrap()

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workflow permissions through the identity capability."""
        self._authorize(principal_id, permission)

    def list_workflows(self) -> tuple[WorkflowDefinition, ...]:
        """List definitions visible in the active workspace."""
        self._ensure_workflow_runtime()
        self.authorize(
            effective_principal_id("local-developer"), Permission.WORKFLOWS_RUN
        )
        return self.workflows.list_scoped(effective_org_id(), effective_workspace_id())

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition:
        """Load one definition visible in the active workspace."""
        self._ensure_workflow_runtime()
        self.authorize(
            effective_principal_id("local-developer"), Permission.WORKFLOWS_RUN
        )
        return self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )

    def get_current_workflow_version(
        self, workflow: WorkflowDefinition
    ) -> WorkflowVersion:
        """Load the immutable version currently published by a definition."""
        return self.workflow_versions.get_scoped(
            workflow.current_version_id or "",
            workflow.org_id,
            workflow.workspace_id,
        )

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for versioned workflow changes."""
        return self._open_unit_of_work()
