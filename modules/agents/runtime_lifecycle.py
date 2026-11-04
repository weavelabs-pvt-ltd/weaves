"""Agent capability use cases."""

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
    AgentStatus,
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    CapabilityKind,
    MemoryPolicy,
    MemoryScope,
    ModelProfile,
    ModelProfileBinding,
    ModelProfileStatus,
    Permission,
    PluginInstallation,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowVersion,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.agents.runtime_catalog import AgentCatalogMixin
from modules.agents.runtime_versioning import AgentVersionPublisher
from modules.agents.skills import snapshot_agent_skills
from modules.agents.templates import (
    AgentTemplate,
    get_agent_template,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class AgentService(AgentCatalogMixin):
    """Create and version agents through explicit persistence and policy ports."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_versions: Repository[AgentVersion],
        audit_events: Repository[AuditEvent],
        model_profiles: Repository[ModelProfile],
        providers: Repository[ModelProviderContract],
        workflows: Repository[WorkflowDefinition],
        workflow_versions: Repository[WorkflowVersion],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        default_model_profile: Callable[[], ModelProfile],
        default_approval_policy_id: Callable[[], str],
        capability_installation: Callable[..., tuple[PluginInstallation, object]],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind agent repositories and the policies used during publishing."""
        self.agents = agents
        self.agent_versions = agent_versions
        self.audit_events = audit_events
        self.model_profiles = model_profiles
        self.providers = providers
        self.workflows = workflows
        self.workflow_versions = workflow_versions
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._default_model_profile = default_model_profile
        self._default_approval_policy_id = default_approval_policy_id
        self._capability_installation = capability_installation
        self._open_unit_of_work = unit_of_work
        self._version_publisher = AgentVersionPublisher(
            agents=agents,
            agent_versions=agent_versions,
            audit_events=audit_events,
            workflows=workflows,
            workflow_versions=workflow_versions,
            unit_of_work=unit_of_work,
        )

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

    def create_agent_from_template(
        self,
        template_id: str,
        *,
        allowed_capability_ids: tuple[str, ...],
        skill_ids: tuple[str, ...] | None = None,
        name: str | None = None,
        model_profile_id: str | None = None,
        memory_scopes: tuple[MemoryScope, ...] = (MemoryScope.WORKSPACE,),
        budget: AgentBudget | None = None,
        output_schema: dict[str, JsonValue] | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> AgentDefinition:
        """Create an agent using template guidance and explicitly chosen tools."""
        template = get_agent_template(template_id)
        self._validate_template_capabilities(template, allowed_capability_ids)
        return self.create_agent(
            name=name or template.name,
            description=template.description,
            persona=template.persona,
            instructions=template.instructions,
            model_profile_id=model_profile_id,
            allowed_capability_ids=allowed_capability_ids,
            skill_ids=(
                template.recommended_skill_ids if skill_ids is None else skill_ids
            ),
            memory_scopes=memory_scopes,
            budget=budget,
            output_schema=output_schema,
            requested_by_principal_id=requested_by_principal_id,
        )

    @staticmethod
    def _validate_template_capabilities(
        template: AgentTemplate, capability_ids: tuple[str, ...]
    ) -> None:
        """Reject missing, duplicate, or unsupported template tools."""
        if not capability_ids:
            raise ValueError("select at least one installed capability")
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("allowed capabilities must be unique")
        unsupported = set(capability_ids) - set(template.recommended_capability_ids)
        if unsupported:
            raise ValueError("template does not support the selected capabilities")

    def create_agent(
        self,
        *,
        name: str,
        instructions: str,
        persona: str = "A helpful work assistant.",
        description: str = "",
        model_profile_id: str | None = None,
        allowed_capability_ids: tuple[str, ...] = ("knowledge.search",),
        memory_scopes: tuple[MemoryScope, ...] = (MemoryScope.WORKSPACE,),
        budget: AgentBudget | None = None,
        output_schema: dict[str, JsonValue] | None = None,
        skill_ids: tuple[str, ...] = (),
        requested_by_principal_id: str = "local-developer",
    ) -> AgentDefinition:
        """Create an agent and immutable executable/workflow versions."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize(requested_by_principal_id, Permission.AGENTS_MANAGE)
        profile, skills = self._validate_new_agent_configuration(
            model_profile_id, allowed_capability_ids, skill_ids
        )
        definition, agent_version, workflow, workflow_version = (
            self._build_new_agent_records(
                name,
                description,
                instructions,
                persona,
                profile,
                allowed_capability_ids,
                memory_scopes,
                budget,
                output_schema,
                skills,
            )
        )
        self._persist_new_agent_records(
            requested_by_principal_id,
            definition,
            agent_version,
            workflow,
            workflow_version,
        )
        return definition

    def _validate_new_agent_configuration(
        self,
        model_profile_id: str | None,
        capability_ids: tuple[str, ...],
        skill_ids: tuple[str, ...],
    ) -> tuple[ModelProfile, tuple[AgentSkillSnapshot, ...]]:
        """Validate model and capability bindings before agent creation."""
        profile = self.model_profiles.get_scoped(
            model_profile_id or self._default_model_profile().id, effective_org_id()
        )
        provider = self.providers.get_scoped(profile.provider_id, effective_org_id())
        ModelProfileBinding(provider=provider, profile=profile)
        if profile.status is not ModelProfileStatus.ACTIVE:
            raise ValueError("model profile must be active")
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("allowed capabilities must be unique")
        for capability_id in capability_ids:
            self._capability_installation(
                capability_id,
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                require_enabled=True,
                allowed_kinds=(
                    CapabilityKind.CONTEXT_READ,
                    CapabilityKind.ACTION_WRITE,
                ),
            )
        return profile, snapshot_agent_skills(skill_ids)

    def _build_new_agent_records(
        self,
        name: str,
        description: str,
        instructions: str,
        persona: str,
        profile: ModelProfile,
        capability_ids: tuple[str, ...],
        memory_scopes: tuple[MemoryScope, ...],
        budget: AgentBudget | None,
        output_schema: dict[str, JsonValue] | None,
        skills: tuple[AgentSkillSnapshot, ...],
    ) -> tuple[AgentDefinition, AgentVersion, WorkflowDefinition, WorkflowVersion]:
        """Build the first immutable agent and workflow records."""
        now = _now()
        agent_id, version_id = _id(), _id()
        workflow_id, workflow_version_id = _id(), _id()
        agent_version = AgentVersion(
            agent_version_id=version_id,
            agent_id=agent_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            version=1,
            persona=persona,
            instructions=instructions,
            model_profile_id=profile.id,
            skills=skills,
            allowed_capability_ids=capability_ids,
            allowed_workflow_ids=(workflow_id,),
            approval_policy_id=self._default_approval_policy_id(),
            memory_policy=MemoryPolicy(
                allowed_scopes=memory_scopes,
                max_items=0 if memory_scopes == (MemoryScope.NONE,) else 8,
            ),
            budget=budget or AgentBudget(),
            output_schema=output_schema,
            created_at=now,
        )
        definition = AgentDefinition(
            id=agent_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            name=name,
            description=description,
            current_version_id=version_id,
            status=AgentStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        workflow_version = WorkflowVersion(
            workflow_version_id=workflow_version_id,
            workflow_id=workflow_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            version=1,
            handler_key="agent.execute",
            agent_version_ids=(version_id,),
            required_capability_ids=capability_ids,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            created_at=now,
        )
        workflow = WorkflowDefinition(
            workflow_id=workflow_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            name=f"Run {name}",
            current_version_id=workflow_version_id,
            status=WorkflowStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        return definition, agent_version, workflow, workflow_version

    def _persist_new_agent_records(
        self,
        actor: str,
        definition: AgentDefinition,
        version: AgentVersion,
        workflow: WorkflowDefinition,
        workflow_version: WorkflowVersion,
    ) -> None:
        """Persist agent, initial workflow, and creation audit event."""
        with self._open_unit_of_work():
            self.agent_versions.create(version)
            self.agents.create(definition)
            self.workflow_versions.create(workflow_version)
            self.workflows.create(workflow)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=definition.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=definition.workspace_id,
                    actor_principal_id=actor,
                    action="agent.created",
                    target_type="agent",
                    target_id=definition.id,
                    summary="Agent created",
                    request_id=definition.id,
                    metadata={"agent_version_id": version.agent_version_id},
                    created_at=definition.created_at,
                )
            )

    def update_agent(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        model_profile_id: str | None = None,
        allowed_capability_ids: tuple[str, ...] | None = None,
        memory_scopes: tuple[MemoryScope, ...] | None = None,
        budget: AgentBudget | None = None,
        output_schema: dict[str, JsonValue] | None = None,
        clear_output_schema: bool = False,
        persona: str | None = None,
        skill_ids: tuple[str, ...] | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> AgentDefinition:
        """Update editable metadata and publish a new immutable agent version."""
        if not self._is_bootstrapped():
            self._bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self._authorize(requested_by_principal_id, Permission.AGENTS_MANAGE)
        current = self.agents.get_scoped(
            agent_id, effective_org_id(), effective_workspace_id()
        )
        old_version = self.agent_versions.get_scoped(
            current.current_version_id or "",
            effective_org_id(),
            effective_workspace_id(),
        )
        if clear_output_schema and output_schema is not None:
            raise ValueError("output_schema cannot be set and cleared together")
        profile_id = model_profile_id or old_version.model_profile_id
        profile, capability_ids = self._validate_agent_update_configuration(
            profile_id, allowed_capability_ids, old_version
        )
        now = _now()
        next_version = self._version_publisher.build_agent_version(
            current,
            old_version,
            profile_id=profile.id,
            persona=persona,
            skills=(
                old_version.skills
                if skill_ids is None
                else snapshot_agent_skills(skill_ids)
            ),
            instructions=instructions,
            capability_ids=capability_ids,
            memory_scopes=memory_scopes,
            budget=budget,
            output_schema=output_schema,
            clear_output_schema=clear_output_schema,
            now=now,
        )
        updated_name = current.name if name is None else name
        updated_description = (
            current.description if description is None else description
        )
        return self._publish_agent_revision(
            current,
            old_version,
            next_version,
            updated_name,
            updated_description,
            requested_by_principal_id,
            now,
        )

    def _publish_agent_revision(
        self,
        current: AgentDefinition,
        old_version: AgentVersion,
        next_version: AgentVersion,
        name: str,
        description: str,
        actor: str,
        now: datetime,
    ) -> AgentDefinition:
        """Persist a changed agent revision and any bound workflow versions."""
        prior = old_version.model_dump(
            mode="json", exclude={"agent_version_id", "version", "created_at"}
        )
        next_state = next_version.model_dump(
            mode="json", exclude={"agent_version_id", "version", "created_at"}
        )
        if (
            prior == next_state
            and name == current.name
            and description == current.description
        ):
            return current
        workflow_updates = self._version_publisher.republish_bound_workflows(
            current, next_version, old_version, now
        )
        updated = current.model_copy(
            update={
                "name": name,
                "description": description,
                "current_version_id": next_version.agent_version_id,
                "updated_at": now,
            }
        )
        self._version_publisher.persist_revision(
            current=current,
            next_version=next_version,
            now=now,
            old_version=old_version,
            actor=actor,
            updated=updated,
            workflow_updates=workflow_updates,
        )
        return updated

    def _validate_agent_update_configuration(
        self,
        profile_id: str,
        requested_capabilities: tuple[str, ...] | None,
        old_version: AgentVersion,
    ) -> tuple[ModelProfile, tuple[str, ...]]:
        """Validate the model profile and enabled capabilities for an update."""
        profile = self.model_profiles.get_scoped(profile_id, effective_org_id())
        provider = self.providers.get_scoped(profile.provider_id, effective_org_id())
        ModelProfileBinding(provider=provider, profile=profile)
        if profile.status is not ModelProfileStatus.ACTIVE:
            raise ValueError("model profile must be active")
        capability_ids = (
            requested_capabilities
            if requested_capabilities is not None
            else old_version.allowed_capability_ids
        )
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("allowed capabilities must be unique")
        for capability_id in capability_ids:
            self._capability_installation(
                capability_id,
                org_id=old_version.org_id,
                workspace_id=old_version.workspace_id,
                require_enabled=True,
                allowed_kinds=(
                    CapabilityKind.CONTEXT_READ,
                    CapabilityKind.ACTION_WRITE,
                ),
            )
        return profile, capability_ids
