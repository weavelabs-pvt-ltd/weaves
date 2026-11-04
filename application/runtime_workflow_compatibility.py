"""Legacy workflow runtime delegates."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING

from contracts.v1 import (
    WorkflowDefinition,
    WorkflowParallelGroup,
    WorkflowStatus,
    WorkflowStepCondition,
    WorkflowStepDependency,
    WorkflowVersion,
)
from modules.workflows.runtime_result import RuntimeResult

if TYPE_CHECKING:
    from modules.workflows.definition_service import WorkflowDefinitionService
    from modules.workflows.execution_service import WorkflowExecutionService


class RuntimeWorkflowCompatibilityMixin:
    """Preserve the historical workflow API through capability services."""

    workflow_definition_service: WorkflowDefinitionService
    workflow_execution_service: WorkflowExecutionService

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
        """Delegate workflow execution while preserving runtime callers."""
        return self.workflow_execution_service.run_workflow(
            workflow_id,
            task,
            requested_by_principal_id,
            thread_id,
            idempotency_key=idempotency_key,
            pinned_workflow_version_id=pinned_workflow_version_id,
            _cancellation_event=_cancellation_event,
            _cancellation_check=_cancellation_check,
        )

    def create_workflow(
        self,
        *,
        name: str,
        agent_id: str | None = None,
        agent_ids: tuple[str, ...] | None = None,
        description: str = "",
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowDefinition:
        """Delegate workflow creation to its capability service."""
        return self.workflow_definition_service.create_workflow(
            name=name,
            agent_id=agent_id,
            agent_ids=agent_ids,
            description=description,
            requested_by_principal_id=requested_by_principal_id,
        )

    def update_workflow(
        self,
        workflow_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: WorkflowStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowDefinition:
        """Delegate workflow metadata updates to its capability service."""
        return self.workflow_definition_service.update_workflow(
            workflow_id,
            name=name,
            description=description,
            status=status,
            requested_by_principal_id=requested_by_principal_id,
        )

    def get_workflow_version(
        self,
        workflow_id: str,
        workflow_version_id: str,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowVersion:
        """Delegate historical workflow version lookup."""
        return self.workflow_definition_service.get_workflow_version(
            workflow_id, workflow_version_id, requested_by_principal_id
        )

    def list_workflow_versions(
        self,
        workflow_id: str,
        *,
        limit: int = 50,
        before_version: int | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[WorkflowVersion, ...]:
        """Delegate workflow version pagination."""
        return self.workflow_definition_service.list_workflow_versions(
            workflow_id,
            limit=limit,
            before_version=before_version,
            requested_by_principal_id=requested_by_principal_id,
        )

    def replace_workflow_agents(
        self,
        workflow_id: str,
        agent_ids: tuple[str, ...],
        step_conditions: tuple[WorkflowStepCondition, ...] = (),
        *,
        parallel_groups: tuple[WorkflowParallelGroup, ...] = (),
        step_dependencies: tuple[WorkflowStepDependency, ...] = (),
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowDefinition:
        """Delegate versioned workflow agent replacement."""
        return self.workflow_definition_service.replace_workflow_agents(
            workflow_id,
            agent_ids,
            step_conditions,
            parallel_groups=parallel_groups,
            step_dependencies=step_dependencies,
            requested_by_principal_id=requested_by_principal_id,
        )
