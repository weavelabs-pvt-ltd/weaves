"""Publish immutable agent versions and their dependent workflow versions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from pydantic import JsonValue

from application.ports.repository import Repository
from contracts.v1 import (
    AgentBudget,
    AgentDefinition,
    AgentSkillSnapshot,
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    MemoryPolicy,
    MemoryScope,
    WorkflowDefinition,
    WorkflowVersion,
)
from contracts.v1.json_data import thaw_json_value
from shared.clock import new_id


class AgentVersionPublisher:
    """Create agent revisions and atomically republish bound workflows."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_versions: Repository[AgentVersion],
        audit_events: Repository[AuditEvent],
        workflows: Repository[WorkflowDefinition],
        workflow_versions: Repository[WorkflowVersion],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind repositories and the transaction boundary for version writes."""
        self._agents = agents
        self._agent_versions = agent_versions
        self._audit_events = audit_events
        self._workflows = workflows
        self._workflow_versions = workflow_versions
        self._unit_of_work = unit_of_work

    @staticmethod
    def build_agent_version(
        current: AgentDefinition,
        old_version: AgentVersion,
        *,
        profile_id: str,
        persona: str | None,
        skills: tuple[AgentSkillSnapshot, ...],
        instructions: str | None,
        capability_ids: tuple[str, ...],
        memory_scopes: tuple[MemoryScope, ...] | None,
        budget: AgentBudget | None,
        output_schema: dict[str, JsonValue] | None,
        clear_output_schema: bool,
        now: datetime,
    ) -> AgentVersion:
        """Create an immutable revision using the requested editable fields."""
        return AgentVersion(
            agent_version_id=new_id(),
            agent_id=current.id,
            org_id=current.org_id,
            workspace_id=current.workspace_id,
            version=old_version.version + 1,
            persona=persona or old_version.persona,
            instructions=(
                old_version.instructions if instructions is None else instructions
            ),
            model_profile_id=profile_id,
            skills=skills,
            allowed_capability_ids=capability_ids,
            allowed_workflow_ids=old_version.allowed_workflow_ids,
            approval_policy_id=old_version.approval_policy_id,
            budget_policy_id=old_version.budget_policy_id,
            memory_policy=(
                old_version.memory_policy
                if memory_scopes is None
                else MemoryPolicy(
                    allowed_scopes=memory_scopes,
                    max_items=0 if memory_scopes == (MemoryScope.NONE,) else 8,
                )
            ),
            budget=budget or old_version.budget,
            output_schema=(
                None
                if clear_output_schema
                else thaw_json_value(old_version.output_schema)
                if output_schema is None
                else output_schema
            ),
            created_at=now,
        )

    def republish_bound_workflows(
        self,
        current: AgentDefinition,
        next_version: AgentVersion,
        old_version: AgentVersion,
        now: datetime,
    ) -> list[tuple[WorkflowDefinition, WorkflowVersion]]:
        """Create workflow revisions that bind to the new agent revision."""
        updates: list[tuple[WorkflowDefinition, WorkflowVersion]] = []
        for workflow_id in old_version.allowed_workflow_ids:
            workflow = self._workflows.get_scoped(
                workflow_id, current.org_id, current.workspace_id
            )
            old_workflow_version = self._workflow_versions.get_scoped(
                workflow.current_version_id or "",
                current.org_id,
                current.workspace_id,
            )
            agent_version_ids = tuple(
                next_version.agent_version_id
                if version_id == old_version.agent_version_id
                else version_id
                for version_id in old_workflow_version.agent_version_ids
            )
            capabilities = self._workflow_capabilities(
                agent_version_ids, current, next_version
            )
            next_workflow_version = WorkflowVersion(
                workflow_version_id=new_id(),
                workflow_id=workflow_id,
                org_id=current.org_id,
                workspace_id=current.workspace_id,
                version=old_workflow_version.version + 1,
                handler_key=old_workflow_version.handler_key,
                agent_version_ids=agent_version_ids,
                required_capability_ids=capabilities,
                step_conditions=old_workflow_version.step_conditions,
                parallel_groups=old_workflow_version.parallel_groups,
                step_dependencies=old_workflow_version.step_dependencies,
                input_schema=thaw_json_value(old_workflow_version.input_schema),
                output_schema=thaw_json_value(old_workflow_version.output_schema),
                configuration=thaw_json_value(old_workflow_version.configuration),
                created_at=now,
            )
            updated_workflow = WorkflowDefinition.model_validate(
                workflow.model_copy(
                    update={
                        "current_version_id": next_workflow_version.workflow_version_id,
                        "updated_at": now,
                    }
                ).model_dump()
            )
            updates.append((updated_workflow, next_workflow_version))
        return updates

    def _workflow_capabilities(
        self,
        version_ids: tuple[str, ...],
        current: AgentDefinition,
        next_version: AgentVersion,
    ) -> tuple[str, ...]:
        capabilities = (
            next_version.allowed_capability_ids
            if version_id == next_version.agent_version_id
            else self._agent_versions.get_scoped(
                version_id, current.org_id, current.workspace_id
            ).allowed_capability_ids
            for version_id in version_ids
        )
        return tuple(dict.fromkeys(item for group in capabilities for item in group))

    def persist_revision(
        self,
        *,
        current: AgentDefinition,
        old_version: AgentVersion,
        next_version: AgentVersion,
        updated: AgentDefinition,
        workflow_updates: list[tuple[WorkflowDefinition, WorkflowVersion]],
        actor: str,
        now: datetime,
    ) -> None:
        """Persist the agent and workflow revisions with one audit record."""
        with self._unit_of_work():
            self._agent_versions.create(next_version)
            self._agents.put(updated)
            for workflow, version in workflow_updates:
                self._workflow_versions.create(version)
                self._workflows.put(workflow)
            self._audit_events.create(
                AuditEvent(
                    audit_event_id=new_id(),
                    org_id=current.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=current.workspace_id,
                    actor_principal_id=actor,
                    action="agent.version.published",
                    target_type="agent",
                    target_id=current.id,
                    summary="Agent version published",
                    request_id=next_version.agent_version_id,
                    metadata={
                        "agent_version_id": next_version.agent_version_id,
                        "workflow_ids": list(old_version.allowed_workflow_ids),
                        "workflow_version_ids": [
                            version.workflow_version_id
                            for _, version in workflow_updates
                        ],
                    },
                    created_at=now,
                )
            )
