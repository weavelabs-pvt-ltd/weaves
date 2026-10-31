"""Atomic persistence operations for workflow replacement."""

from __future__ import annotations

from typing import Any

from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
)
from modules.workflows.runtime_definition_host import WorkflowDefinitionRuntimeHost
from shared.clock import new_id as _id


class WorkflowReplacementPersistenceMixin(WorkflowDefinitionRuntimeHost):
    """Persist replacement publications with their audit trail."""

    def _persist_replaced_workflow_agents(
        self,
        actor: Any,
        changed_definitions: Any,
        current_workflow_version: Any,
        next_workflow: Any,
        next_workflow_version: Any,
        now: Any,
        replacements: Any,
        selected: Any,
        selected_version_ids: Any,
        workflow: Any,
        workflow_publications: Any,
        workflow_id: Any,
    ) -> None:
        """Persist republished workflows after agent replacement."""
        with self._unit_of_work():
            self._persist_replacement_agent_changes(
                actor, changed_definitions, replacements, workflow, workflow_id, now
            )
            self._persist_linked_workflow_changes(
                actor, workflow_publications, workflow, now
            )
            self._persist_replacement_workflow(
                actor,
                current_workflow_version,
                next_workflow,
                next_workflow_version,
                selected,
                selected_version_ids,
                workflow,
                workflow_id,
                now,
            )

    def _persist_replacement_agent_changes(
        self,
        actor: Any,
        changed_definitions: Any,
        replacements: Any,
        workflow: Any,
        workflow_id: str,
        now: Any,
    ) -> None:
        """Persist newly published agent versions and access audit events."""
        for replacement in replacements.values():
            self.agent_versions.create(replacement)
        for definition in changed_definitions.values():
            self.agents.put(definition)
        for previous_id, replacement in replacements.items():
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=workflow.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=workflow.workspace_id,
                    actor_principal_id=actor,
                    action="agent.workflow_access.published",
                    target_type="agent",
                    target_id=replacement.agent_id,
                    summary="Agent version published for workflow access",
                    request_id=replacement.agent_version_id,
                    metadata={
                        "previous_agent_version_id": previous_id,
                        "agent_version_id": replacement.agent_version_id,
                        "workflow_id": workflow_id,
                    },
                    created_at=now,
                )
            )

    def _persist_linked_workflow_changes(
        self, actor: Any, publications: Any, workflow: Any, now: Any
    ) -> None:
        """Persist dependent workflow versions and audit events."""
        for definition, version in publications.values():
            self.workflow_versions.create(version)
            self.workflows.put(definition)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=workflow.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=workflow.workspace_id,
                    actor_principal_id=actor,
                    action="workflow.version.published",
                    target_type="workflow",
                    target_id=definition.workflow_id,
                    summary="Workflow agent bindings published",
                    request_id=version.workflow_version_id,
                    metadata={
                        "workflow_version_id": version.workflow_version_id,
                        "agent_version_ids": list(version.agent_version_ids),
                    },
                    created_at=now,
                )
            )

    def _persist_replacement_workflow(
        self,
        actor: Any,
        current_version: Any,
        definition: Any,
        version: Any,
        selected: Any,
        selected_version_ids: Any,
        workflow: Any,
        workflow_id: str,
        now: Any,
    ) -> None:
        """Persist the requested workflow version and audit its publication."""
        self.workflow_versions.create(version)
        self.workflows.put(definition)
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=workflow.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=workflow.workspace_id,
                actor_principal_id=actor,
                action="workflow.steps.published",
                target_type="workflow",
                target_id=workflow_id,
                summary="Workflow agent sequence published",
                request_id=version.workflow_version_id,
                metadata={
                    "previous_workflow_version_id": current_version.workflow_version_id,
                    "workflow_version_id": version.workflow_version_id,
                    "agent_ids": [item.id for item, _ in selected],
                    "agent_version_ids": list(selected_version_ids),
                },
                created_at=now,
            )
        )
