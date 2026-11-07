"""Read and lifecycle operations for workspace agent catalog entries."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    Permission,
)
from modules.agents.skills import AgentSkill, list_agent_skills
from modules.agents.templates import AgentTemplate, list_agent_templates
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class AgentCatalogMixin:
    """Provide catalog reads, version history, and lifecycle transitions."""

    agents: Repository[AgentDefinition]
    agent_versions: Repository[AgentVersion]
    audit_events: Repository[AuditEvent]
    _is_bootstrapped: Callable[[], bool]
    _bootstrap: Callable[[], None]
    _authorize: Callable[[str, Permission], None]
    _open_unit_of_work: Callable[[], AbstractContextManager[None]]

    def get_agent_templates(self) -> tuple[AgentTemplate, ...]:
        """List built-in agent templates visible to the current workspace."""
        self._authorize("local-developer", Permission.AGENTS_VIEW)
        return list_agent_templates()

    def get_agent_skills(self) -> tuple[AgentSkill, ...]:
        """List reusable instructions without exposing tools or credentials."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize("local-developer", Permission.AGENTS_VIEW)
        return list_agent_skills()

    def list_agents(self) -> tuple[AgentDefinition, ...]:
        """List agents visible in the active workspace."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize("local-developer", Permission.AGENTS_VIEW)
        return self.agents.list_scoped(effective_org_id(), effective_workspace_id())

    def get_agent(self, agent_id: str) -> AgentDefinition:
        """Load one agent after enforcing workspace view access."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize("local-developer", Permission.AGENTS_VIEW)
        return self.agents.get_scoped(
            agent_id, effective_org_id(), effective_workspace_id()
        )

    def get_agent_version(self, version_id: str) -> AgentVersion:
        """Load a version visible to the active organization and workspace."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize("local-developer", Permission.AGENTS_VIEW)
        return self.agent_versions.get_scoped(
            version_id, effective_org_id(), effective_workspace_id()
        )

    def list_agent_versions(self, agent_id: str) -> tuple[AgentVersion, ...]:
        """List immutable versions for a visible agent, newest first."""
        if not self._is_bootstrapped():
            self._bootstrap()
        self._authorize("local-developer", Permission.AGENTS_VIEW)
        agent = self.agents.get_scoped(
            agent_id, effective_org_id(), effective_workspace_id()
        )
        return tuple(
            sorted(
                (
                    version
                    for version in self.agent_versions.list_scoped(
                        effective_org_id(), effective_workspace_id()
                    )
                    if version.agent_id == agent.id
                ),
                key=lambda version: version.version,
                reverse=True,
            )
        )

    def set_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> AgentDefinition:
        """Archive or reactivate a configured agent with an audit event."""
        if status not in {AgentStatus.ACTIVE, AgentStatus.ARCHIVED}:
            raise ValueError("agents can only be activated or archived")
        if not self._is_bootstrapped():
            self._bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self._authorize(actor, Permission.AGENTS_MANAGE)
        current = self.agents.get_scoped(
            agent_id, effective_org_id(), effective_workspace_id()
        )
        if status is AgentStatus.ACTIVE and current.current_version_id is None:
            raise ValueError("an agent needs a published version before activation")
        if current.status is status:
            return current
        now = _now()
        updated = AgentDefinition.model_validate(
            current.model_copy(
                update={"status": status, "updated_at": now}
            ).model_dump()
        )
        with self._open_unit_of_work():
            self.agents.put(updated)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=current.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=current.workspace_id,
                    actor_principal_id=actor,
                    action=(
                        "agent.archived"
                        if status is AgentStatus.ARCHIVED
                        else "agent.activated"
                    ),
                    target_type="agent",
                    target_id=current.id,
                    summary=(
                        "Agent archived"
                        if status is AgentStatus.ARCHIVED
                        else "Agent activated"
                    ),
                    request_id=current.id,
                    metadata={"status": status.value},
                    created_at=now,
                )
            )
        return updated


__all__ = ["AgentCatalogMixin"]
