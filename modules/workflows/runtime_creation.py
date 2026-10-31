"""Workflow creation and publication use cases."""

from __future__ import annotations

from datetime import datetime

from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    Permission,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowVersion,
)
from modules.workflows.runtime_definition_host import WorkflowDefinitionRuntimeHost
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowCreationMixin(WorkflowDefinitionRuntimeHost):
    """Create workflows and atomically publish their access bindings."""

    def create_workflow(
        self,
        *,
        name: str,
        agent_id: str | None = None,
        agent_ids: tuple[str, ...] | None = None,
        description: str = "",
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowDefinition:
        """Publish a linear workflow from one or more current agent versions."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_MANAGE)
        selected_ids = self._normalize_selected_agent_ids(agent_id, agent_ids)
        selected = self._select_workflow_agents(selected_ids)
        normalized_name = name.strip()
        self._ensure_workflow_name_available(normalized_name)
        now = _now()
        workflow_id = _id()
        agent_updates, version_replacements = self._prepare_agent_publications(
            selected, workflow_id, now
        )
        linked_workflows = self._prepare_linked_workflow_publications(
            version_replacements, now
        )
        workflow_version = self._build_workflow_version(workflow_id, agent_updates, now)
        workflow = WorkflowDefinition(
            workflow_id=workflow_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            name=normalized_name,
            description=description.strip(),
            current_version_id=workflow_version.workflow_version_id,
            status=WorkflowStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self._persist_workflow_creation(
            actor,
            workflow,
            workflow_version,
            selected,
            agent_updates,
            linked_workflows,
            now,
        )
        return workflow

    def _ensure_workflow_runtime(self) -> None:
        """Bootstrap the runtime before a workflow operation."""
        if not self._is_bootstrapped():
            self.bootstrap()

    @staticmethod
    def _normalize_selected_agent_ids(
        agent_id: str | None, agent_ids: tuple[str, ...] | None
    ) -> tuple[str, ...]:
        """Resolve the legacy single-agent argument into the ordered list."""
        if agent_id is not None and agent_ids is not None:
            raise ValueError("choose either agent_id or agent_ids")
        return agent_ids or ((agent_id,) if agent_id else ())

    def _select_workflow_agents(
        self, selected_agent_ids: tuple[str, ...]
    ) -> list[tuple[AgentDefinition, AgentVersion]]:
        """Load and validate active, current agent versions for a workflow."""
        if not 1 <= len(selected_agent_ids) <= 10:
            raise ValueError("workflows require one to ten agent IDs")
        if len(selected_agent_ids) != len(set(selected_agent_ids)):
            raise ValueError("workflow agent IDs must be unique")
        selected: list[tuple[AgentDefinition, AgentVersion]] = []
        for agent_id in selected_agent_ids:
            agent = self.agents.get_scoped(
                agent_id, effective_org_id(), effective_workspace_id()
            )
            if agent.status is not AgentStatus.ACTIVE:
                raise ValueError("all workflow agents must be active")
            version = self.agent_versions.get_scoped(
                agent.current_version_id or "", agent.org_id, agent.workspace_id
            )
            selected.append((agent, version))
        return selected

    def _ensure_workflow_name_available(self, name: str) -> None:
        """Reject a duplicate workflow name in the active workspace."""
        if any(
            workflow.name.casefold() == name.casefold()
            for workflow in self.workflows.list_scoped(
                effective_org_id(), effective_workspace_id()
            )
        ):
            raise ValueError("workflow name already exists in this workspace")

    @staticmethod
    def _publish_agent_version(
        agent: AgentDefinition,
        version: AgentVersion,
        workflow_id: str,
        now: datetime,
    ) -> tuple[AgentDefinition, AgentVersion]:
        """Return an agent and immutable version granting workflow access."""
        published = AgentVersion.model_validate(
            version.model_copy(
                update={
                    "agent_version_id": _id(),
                    "version": version.version + 1,
                    "allowed_workflow_ids": (
                        *version.allowed_workflow_ids,
                        workflow_id,
                    ),
                    "created_at": now,
                }
            ).model_dump()
        )
        updated = AgentDefinition.model_validate(
            agent.model_copy(
                update={
                    "current_version_id": published.agent_version_id,
                    "updated_at": now,
                }
            ).model_dump()
        )
        return updated, published

    def _prepare_agent_publications(
        self,
        selected: list[tuple[AgentDefinition, AgentVersion]],
        workflow_id: str,
        now: datetime,
    ) -> tuple[list[tuple[AgentDefinition, AgentVersion]], dict[str, str]]:
        """Prepare updated agent definitions and old to new version mappings."""
        updates: list[tuple[AgentDefinition, AgentVersion]] = []
        replacements: dict[str, str] = {}
        for agent, old_version in selected:
            updated, published = self._publish_agent_version(
                agent, old_version, workflow_id, now
            )
            replacements[old_version.agent_version_id] = published.agent_version_id
            updates.append((updated, published))
        return updates, replacements

    def _prepare_linked_workflow_publications(
        self, replacements: dict[str, str], now: datetime
    ) -> list[tuple[WorkflowDefinition, WorkflowVersion]]:
        """Republish existing workflows affected by bumped agent versions."""
        publications: list[tuple[WorkflowDefinition, WorkflowVersion]] = []
        for linked in self.workflows.list_scoped(
            effective_org_id(), effective_workspace_id()
        ):
            version_id = linked.current_version_id
            if version_id is None:
                continue
            current = self.workflow_versions.get_scoped(
                version_id, linked.org_id, linked.workspace_id
            )
            agent_version_ids = tuple(
                replacements.get(item, item) for item in current.agent_version_ids
            )
            if agent_version_ids == current.agent_version_ids:
                continue
            published = WorkflowVersion.model_validate(
                current.model_copy(
                    update={
                        "workflow_version_id": _id(),
                        "version": current.version + 1,
                        "agent_version_ids": agent_version_ids,
                        "created_at": now,
                    }
                ).model_dump()
            )
            updated = WorkflowDefinition.model_validate(
                linked.model_copy(
                    update={
                        "current_version_id": published.workflow_version_id,
                        "updated_at": now,
                    }
                ).model_dump()
            )
            publications.append((updated, published))
        return publications

    @staticmethod
    def _build_workflow_version(
        workflow_id: str,
        updates: list[tuple[AgentDefinition, AgentVersion]],
        now: datetime,
    ) -> WorkflowVersion:
        """Build the first immutable workflow version from published agents."""
        agent_version_ids = tuple(version.agent_version_id for _, version in updates)
        capabilities = tuple(
            dict.fromkeys(
                capability_id
                for _, version in updates
                for capability_id in version.allowed_capability_ids
            )
        )
        return WorkflowVersion(
            workflow_version_id=_id(),
            workflow_id=workflow_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            version=1,
            handler_key=(
                "agent.execute" if len(agent_version_ids) == 1 else "agent.sequence"
            ),
            agent_version_ids=agent_version_ids,
            required_capability_ids=capabilities,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            created_at=now,
        )

    def _persist_workflow_creation(
        self,
        actor: str,
        workflow: WorkflowDefinition,
        workflow_version: WorkflowVersion,
        selected: list[tuple[AgentDefinition, AgentVersion]],
        agent_updates: list[tuple[AgentDefinition, AgentVersion]],
        linked_workflows: list[tuple[WorkflowDefinition, WorkflowVersion]],
        now: datetime,
    ) -> None:
        """Persist all workflow and agent publications atomically."""
        with self._unit_of_work():
            self._persist_agent_publications(
                actor, selected, agent_updates, workflow.workflow_id, now
            )
            self._persist_linked_workflows(actor, linked_workflows, now)
            self.workflow_versions.create(workflow_version)
            self.workflows.create(workflow)
            self._audit_created_workflow(
                actor, workflow, workflow_version, selected, now
            )

    def _persist_agent_publications(
        self,
        actor: str,
        selected: list[tuple[AgentDefinition, AgentVersion]],
        updates: list[tuple[AgentDefinition, AgentVersion]],
        workflow_id: str,
        now: datetime,
    ) -> None:
        """Store the new agent versions and their access audit records."""
        for (updated, published), (_, previous) in zip(updates, selected, strict=True):
            self.agent_versions.create(published)
            self.agents.put(updated)
            self.audit_events.create(
                self._agent_access_audit(
                    actor,
                    updated,
                    published,
                    previous.agent_version_id,
                    workflow_id,
                    now,
                )
            )

    @staticmethod
    def _agent_access_audit(
        actor: str,
        agent: AgentDefinition,
        version: AgentVersion,
        previous_id: str,
        workflow_id: str,
        now: datetime,
    ) -> AuditEvent:
        """Build an audit event for workflow access being published."""
        return AuditEvent(
            audit_event_id=_id(),
            org_id=agent.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=agent.workspace_id,
            actor_principal_id=actor,
            action="agent.workflow_access.published",
            target_type="agent",
            target_id=agent.id,
            summary="Agent version published for workflow access",
            request_id=version.agent_version_id,
            metadata={
                "previous_agent_version_id": previous_id,
                "agent_version_id": version.agent_version_id,
                "workflow_id": workflow_id,
            },
            created_at=now,
        )

    def _persist_linked_workflows(
        self,
        actor: str,
        publications: list[tuple[WorkflowDefinition, WorkflowVersion]],
        now: datetime,
    ) -> None:
        """Store dependent workflow versions and audit records."""
        for workflow, version in publications:
            self.workflow_versions.create(version)
            self.workflows.put(workflow)
            self.audit_events.create(
                self._linked_workflow_audit(actor, workflow, version, now)
            )

    @staticmethod
    def _linked_workflow_audit(
        actor: str,
        workflow: WorkflowDefinition,
        version: WorkflowVersion,
        now: datetime,
    ) -> AuditEvent:
        """Build an audit event for a dependent workflow version."""
        return AuditEvent(
            audit_event_id=_id(),
            org_id=workflow.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=workflow.workspace_id,
            actor_principal_id=actor,
            action="workflow.version.published",
            target_type="workflow",
            target_id=workflow.workflow_id,
            summary="Workflow agent bindings published",
            request_id=version.workflow_version_id,
            metadata={
                "workflow_version_id": version.workflow_version_id,
                "agent_version_ids": list(version.agent_version_ids),
            },
            created_at=now,
        )

    def _audit_created_workflow(
        self,
        actor: str,
        workflow: WorkflowDefinition,
        version: WorkflowVersion,
        selected: list[tuple[AgentDefinition, AgentVersion]],
        now: datetime,
    ) -> None:
        """Record the creation and initial publication of a workflow."""
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=workflow.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=workflow.workspace_id,
                actor_principal_id=actor,
                action="workflow.created",
                target_type="workflow",
                target_id=workflow.workflow_id,
                summary="Workflow created and published",
                request_id=workflow.workflow_id,
                metadata={
                    "agent_ids": [agent.id for agent, _ in selected],
                    "agent_version_ids": list(version.agent_version_ids),
                    "workflow_version_id": version.workflow_version_id,
                },
                created_at=now,
            )
        )
