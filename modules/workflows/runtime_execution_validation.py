"""Load and validate immutable workflow execution configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    ModelProfileBinding,
    ModelProfileStatus,
    ModelProviderStatus,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowStepCondition,
    WorkflowVersion,
)
from modules.workflows.runtime_execution_host import WorkflowExecutionRuntimeHost
from shared.request_context import effective_org_id, effective_workspace_id


@dataclass
class WorkflowExecutionPlan:
    """Validated, immutable records required to execute one workflow version."""

    workflow: WorkflowDefinition
    version: WorkflowVersion
    agents: list[AgentDefinition]
    agent_versions: list[AgentVersion]
    conditions_by_target: dict[str, WorkflowStepCondition]
    dependencies_by_target: dict[str, Any]
    groups_by_start: dict[int, tuple[int, ...]]
    group_members: set[int]


class WorkflowExecutionValidationMixin(WorkflowExecutionRuntimeHost):
    """Validate the agent and graph configuration pinned to a workflow run."""

    def _load_sequence_agent_versions(
        self,
        agent_definitions: Any,
        agent_version_ids: Any,
        agent_versions: Any,
        available_capabilities: Any,
        workflow: Any,
        pinned_workflow_version_id: Any,
    ) -> tuple[Any, ...]:
        """Load and validate the agent versions in a workflow."""
        for version_id in agent_version_ids:
            agent_version = self.agent_versions.get_scoped(
                version_id, workflow.org_id, workflow.workspace_id
            )
            if workflow.workflow_id not in agent_version.allowed_workflow_ids:
                raise ValueError(
                    "workflow is not allowed by one of its published agent versions"
                )
            definition = self.agents.get_scoped(
                agent_version.agent_id, workflow.org_id, workflow.workspace_id
            )
            if definition.status is not AgentStatus.ACTIVE:
                raise ValueError("all workflow agents must be active")
            if (
                pinned_workflow_version_id is None
                and definition.current_version_id != agent_version.agent_version_id
            ):
                raise ValueError("workflow references an outdated agent version")
            profile = self.model_profiles.get_scoped(
                agent_version.model_profile_id, workflow.org_id
            )
            provider = self.providers.get_scoped(profile.provider_id, workflow.org_id)
            try:
                access_mode = self.model_configurations.get_scoped(
                    workflow.org_id, workflow.org_id
                ).mode
            except KeyError:
                access_mode = None
            ModelProfileBinding(
                provider=provider, profile=profile, access_mode=access_mode
            )
            if (
                definition.status is not AgentStatus.ACTIVE
                or profile.status is not ModelProfileStatus.ACTIVE
                or provider.status is not ModelProviderStatus.ACTIVE
            ):
                raise ValueError("all workflow agents need active model configuration")
            if (
                agent_version.budget.max_run_cost_usd is not None
                or profile.cost_budget_usd is not None
            ) and (
                profile.input_cost_per_million_tokens_usd is None
                or profile.output_cost_per_million_tokens_usd is None
            ):
                raise ValueError(
                    "Cost budgets require input and output token rates "
                    "on the model profile"
                )
            available_capabilities.update(agent_version.allowed_capability_ids)
            agent_versions.append(agent_version)
            agent_definitions.append(definition)
        return agent_version, definition

    def _validate_workflow_dependencies(
        self,
        agent_definitions: Any,
        dependencies_by_target: Any,
        step_index_by_agent_id: Any,
        workflow_version: Any,
    ) -> None:
        """Reject workflow dependency graphs that cannot run."""
        if workflow_version.step_dependencies:
            if not set(dependencies_by_target).issubset(
                {item.id for item in agent_definitions[1:]}
            ):
                raise ValueError("workflow contains an invalid dependency target")
            for dependency in workflow_version.step_dependencies:
                target_index = step_index_by_agent_id.get(dependency.target_agent_id)
                if target_index is None or target_index == 0:
                    raise ValueError("workflow contains an invalid dependency target")
                if any(
                    step_index_by_agent_id.get(source_id, target_index) >= target_index
                    for source_id in dependency.depends_on_agent_ids
                ):
                    raise ValueError("workflow contains an invalid dependency source")
            if any(
                condition.source_agent_id
                not in set(
                    dependencies_by_target[
                        condition.target_agent_id
                    ].depends_on_agent_ids
                )
                for condition in workflow_version.step_conditions
            ):
                raise ValueError(
                    "workflow condition source is not a declared dependency"
                )

    def _index_parallel_groups(
        self,
        parallel_group_members: Any,
        parallel_groups_by_start: Any,
        step_index_by_agent_id: Any,
        workflow_version: Any,
    ) -> None:
        """Index parallel groups by their starting step."""
        for group in workflow_version.parallel_groups:
            group_indexes = tuple(
                step_index_by_agent_id.get(agent_id, -1) for agent_id in group.agent_ids
            )
            if (
                -1 in group_indexes
                or group_indexes[0] == 0
                or group_indexes
                != tuple(range(group_indexes[0], group_indexes[0] + len(group_indexes)))
                or group_indexes[0] in parallel_group_members
                or parallel_group_members.intersection(group_indexes)
            ):
                raise ValueError("workflow contains an invalid parallel group")
            group_agent_ids = set(group.agent_ids)
            if any(
                condition.target_agent_id in group_agent_ids
                and condition.source_agent_id in group_agent_ids
                for condition in workflow_version.step_conditions
            ):
                raise ValueError("parallel steps cannot depend on another group step")
            if any(
                dependency.target_agent_id in group_agent_ids
                and set(dependency.depends_on_agent_ids).intersection(group_agent_ids)
                for dependency in workflow_version.step_dependencies
            ):
                raise ValueError("parallel steps cannot depend on another group step")
            parallel_groups_by_start[group_indexes[0]] = group_indexes
            parallel_group_members.update(group_indexes)

    def _load_workflow_execution_plan(
        self,
        workflow_id: str,
        thread_id: str | None,
        pinned_version_id: str | None,
    ) -> WorkflowExecutionPlan:
        """Load scoped workflow records and validate its published graph."""
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        if workflow.status is not WorkflowStatus.ACTIVE:
            raise ValueError("workflow must be active to run")
        if workflow.current_version_id is None and pinned_version_id is None:
            raise ValueError("workflow has no published version")
        version = self.workflow_versions.get_scoped(
            pinned_version_id or workflow.current_version_id or "",
            workflow.org_id,
            workflow.workspace_id,
        )
        self._validate_workflow_handler(workflow_id, version)
        agents, versions = self._load_workflow_agents(
            workflow, version, pinned_version_id
        )
        indexes = {agent.id: index for index, agent in enumerate(agents)}
        conditions = self._index_workflow_conditions(version, indexes)
        dependencies = {
            item.target_agent_id: item for item in version.step_dependencies
        }
        self._validate_workflow_dependencies(agents, dependencies, indexes, version)
        groups: dict[int, tuple[int, ...]] = {}
        members: set[int] = set()
        self._index_parallel_groups(members, groups, indexes, version)
        self._validate_workflow_thread(thread_id, workflow, agents)
        return WorkflowExecutionPlan(
            workflow=workflow,
            version=version,
            agents=agents,
            agent_versions=versions,
            conditions_by_target=conditions,
            dependencies_by_target=dependencies,
            groups_by_start=groups,
            group_members=members,
        )

    @staticmethod
    def _validate_workflow_handler(workflow_id: str, version: WorkflowVersion) -> None:
        """Check workflow identity, handler, and ordered version count."""
        if version.workflow_id != workflow_id:
            raise ValueError("workflow version does not belong to this workflow")
        count = len(version.agent_version_ids)
        if version.handler_key == "agent.execute" and count != 1:
            raise ValueError("agent.execute workflows must reference one agent version")
        if version.handler_key == "agent.sequence" and not 2 <= count <= 10:
            raise ValueError(
                "agent.sequence workflows require two to ten agent versions"
            )
        if version.handler_key not in {"agent.execute", "agent.sequence"}:
            raise ValueError("workflow handler is not supported by this runtime")

    def _load_workflow_agents(
        self,
        workflow: WorkflowDefinition,
        version: WorkflowVersion,
        pinned_version_id: str | None,
    ) -> tuple[list[AgentDefinition], list[AgentVersion]]:
        """Load pinned agents and ensure their capabilities satisfy the workflow."""
        agents: list[AgentDefinition] = []
        versions: list[AgentVersion] = []
        capabilities: set[str] = set()
        self._load_sequence_agent_versions(
            agents,
            version.agent_version_ids,
            versions,
            capabilities,
            workflow,
            pinned_version_id,
        )
        if set(version.required_capability_ids).difference(capabilities):
            raise ValueError("workflow requires capabilities not allowed by its agents")
        return agents, versions

    @staticmethod
    def _index_workflow_conditions(
        version: WorkflowVersion, indexes: dict[str, int]
    ) -> dict[str, WorkflowStepCondition]:
        """Validate forward condition references and index by target agent."""
        conditions: dict[str, WorkflowStepCondition] = {}
        for condition in version.step_conditions:
            source = indexes.get(condition.source_agent_id)
            target = indexes.get(condition.target_agent_id)
            if source is None or target is None or source >= target:
                raise ValueError("workflow contains an invalid step condition")
            conditions[condition.target_agent_id] = condition
        return conditions

    def _validate_workflow_thread(
        self,
        thread_id: str | None,
        workflow: WorkflowDefinition,
        agents: list[AgentDefinition],
    ) -> None:
        """Ensure a conversation thread belongs to the first workflow agent."""
        if thread_id is None:
            return
        thread = self.threads.get_scoped(
            thread_id, workflow.org_id, workflow.workspace_id
        )
        if thread.agent_id != agents[0].id:
            raise ValueError("thread must be bound to the first workflow agent")
