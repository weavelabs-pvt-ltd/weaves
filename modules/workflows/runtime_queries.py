"""Workflow definition lifecycle use cases."""

from __future__ import annotations

from typing import Any

from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowVersion,
)
from modules.workflows.graph import workflow_graph_view
from modules.workflows.runtime_definition_host import WorkflowDefinitionRuntimeHost
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowQueryMixin(WorkflowDefinitionRuntimeHost):
    """Read workflow definitions, versions, and graph projections."""

    def graph_for_version(self, version: WorkflowVersion) -> dict[str, Any]:
        """Build a graph projection from one scoped, immutable workflow version."""
        agent_versions = tuple(
            self.agent_versions.get_scoped(
                version_id, version.org_id, version.workspace_id
            )
            for version_id in version.agent_version_ids
        )
        agents_by_id = {
            agent_version.agent_id: self.agents.get_scoped(
                agent_version.agent_id, version.org_id, version.workspace_id
            )
            for agent_version in agent_versions
        }
        return workflow_graph_view(version, agent_versions, agents_by_id)

    def update_workflow(
        self,
        workflow_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: WorkflowStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowDefinition:
        """Update workflow metadata or archive/reactivate its published version."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_MANAGE)
        current = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        next_name = name.strip() if name is not None else current.name
        if any(
            item.workflow_id != workflow_id
            and item.name.casefold() == next_name.casefold()
            for item in self.workflows.list_scoped(current.org_id, current.workspace_id)
        ):
            raise ValueError("workflow name already exists in this workspace")
        now = _now()
        updated = WorkflowDefinition.model_validate(
            current.model_copy(
                update={
                    "name": next_name,
                    "description": (
                        description.strip()
                        if description is not None
                        else current.description
                    ),
                    "status": status or current.status,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.workflows.put(updated)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=current.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=current.workspace_id,
                    actor_principal_id=actor,
                    action="workflow.updated",
                    target_type="workflow",
                    target_id=workflow_id,
                    summary="Workflow settings updated",
                    request_id=workflow_id,
                    metadata={"status": updated.status.value},
                    created_at=now,
                )
            )
        return updated

    def get_workflow_version(
        self,
        workflow_id: str,
        workflow_version_id: str,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowVersion:
        """Return a historical version only when it belongs to the scoped workflow."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_VIEW)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        version = self.workflow_versions.get_scoped(
            workflow_version_id, workflow.org_id, workflow.workspace_id
        )
        if version.workflow_id != workflow_id:
            raise KeyError("workflow version not found")
        return version

    def list_workflow_versions(
        self,
        workflow_id: str,
        *,
        limit: int = 50,
        before_version: int | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[WorkflowVersion, ...]:
        """Return a bounded, newest-first page of scoped workflow versions."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_VIEW)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        if not 1 <= limit <= 101:
            raise ValueError("version page limit must be between 1 and 101")
        if before_version is not None and before_version < 1:
            raise ValueError("before_version must be positive")
        versions = [
            item
            for item in self.workflow_versions.list_scoped(
                workflow.org_id, workflow.workspace_id
            )
            if item.workflow_id == workflow_id
            and (before_version is None or item.version < before_version)
        ]
        versions.sort(key=lambda item: item.version, reverse=True)
        return tuple(versions[:limit])
