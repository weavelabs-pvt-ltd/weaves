"""Workflow definition lifecycle use cases."""

from __future__ import annotations

from typing import Any

from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    Permission,
    WorkflowDefinition,
    WorkflowParallelGroup,
    WorkflowStepCondition,
    WorkflowStepDependency,
    WorkflowVersion,
)
from contracts.v1.json_data import thaw_json_value
from modules.workflows.runtime_replacement_persistence import (
    WorkflowReplacementPersistenceMixin,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowReplacementMixin(WorkflowReplacementPersistenceMixin):
    """Replace workflow steps and republish dependent versions."""

    def _validate_parallel_groups(
        self,
        parallel_agent_ids: Any,
        selected_indexes: Any,
        agent_ids: Any,
        parallel_groups: Any,
        step_conditions: Any,
        step_dependencies: Any,
    ) -> None:
        """Validate parallel groups against the selected agents."""
        for group in parallel_groups:
            group_indexes = sorted(
                selected_indexes.get(agent_id, -1) for agent_id in group.agent_ids
            )
            if -1 in group_indexes:
                raise ValueError("parallel group agents must be workflow steps")
            if (
                group_indexes[0] == 0
                or group_indexes != list(range(group_indexes[0], group_indexes[-1] + 1))
                or tuple(agent_ids[group_indexes[0] : group_indexes[-1] + 1])
                != group.agent_ids
            ):
                raise ValueError(
                    "parallel groups must contain adjacent steps after the first step"
                )
            if parallel_agent_ids.intersection(group.agent_ids):
                raise ValueError("parallel groups cannot share workflow steps")
            parallel_agent_ids.update(group.agent_ids)
            if any(
                condition.target_agent_id in group.agent_ids
                and condition.source_agent_id in group.agent_ids
                for condition in step_conditions
            ):
                raise ValueError("parallel steps cannot depend on another group step")
            if any(
                dependency.target_agent_id in group.agent_ids
                and set(dependency.depends_on_agent_ids).intersection(group.agent_ids)
                for dependency in step_dependencies
            ):
                raise ValueError("parallel steps cannot depend on another group step")

    def _publish_replacement_agent_versions(
        self,
        changed_definitions: Any,
        now: Any,
        replacements: Any,
        selected: Any,
        workflow_id: Any,
    ) -> None:
        """Publish a new version for each replaced agent."""
        for definition, version in selected:
            if workflow_id in version.allowed_workflow_ids:
                continue
            replacement = AgentVersion.model_validate(
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
            replacements[version.agent_version_id] = replacement
            changed_definitions[definition.id] = AgentDefinition.model_validate(
                definition.model_copy(
                    update={
                        "current_version_id": replacement.agent_version_id,
                        "updated_at": now,
                    }
                ).model_dump()
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
        """Publish a new workflow version with an explicitly ordered agent list.

        Agent and workflow versions remain immutable. When an agent is newly
        attached to this workflow, a new agent version grants that workflow
        access, and every workflow currently bound to the prior agent version
        is advanced in the same unit of work.
        """
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_MANAGE)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        current_workflow_version, selected = self._load_replacement_selection(
            workflow,
            agent_ids,
            step_conditions,
            parallel_groups,
            step_dependencies,
        )

        if (
            current_workflow_version.agent_version_ids
            == tuple(version.agent_version_id for _, version in selected)
            and current_workflow_version.step_conditions == step_conditions
            and current_workflow_version.parallel_groups == parallel_groups
            and current_workflow_version.step_dependencies == step_dependencies
        ):
            return workflow

        return self._publish_workflow_replacement(
            actor,
            workflow,
            current_workflow_version,
            selected,
            step_conditions,
            parallel_groups,
            step_dependencies,
        )

    def _publish_workflow_replacement(
        self,
        actor: str,
        workflow: WorkflowDefinition,
        current_version: WorkflowVersion,
        selected: list[tuple[AgentDefinition, AgentVersion]],
        step_conditions: tuple[WorkflowStepCondition, ...],
        parallel_groups: tuple[WorkflowParallelGroup, ...],
        step_dependencies: tuple[WorkflowStepDependency, ...],
    ) -> WorkflowDefinition:
        """Prepare, persist, and return a replacement workflow publication."""
        now = _now()
        replacements, changed_definitions, workflow_publications = (
            self._prepare_replacement_publications(selected, workflow, now)
        )

        selected_version_ids = tuple(
            replacements.get(version.agent_version_id, version).agent_version_id
            for _, version in selected
        )
        selected_capabilities = tuple(
            dict.fromkeys(
                capability_id
                for _, version in selected
                for capability_id in replacements.get(
                    version.agent_version_id, version
                ).allowed_capability_ids
            )
        )
        next_workflow_version = WorkflowVersion(
            workflow_version_id=_id(),
            workflow_id=workflow.workflow_id,
            org_id=workflow.org_id,
            workspace_id=workflow.workspace_id,
            version=current_version.version + 1,
            handler_key=(
                "agent.execute" if len(selected_version_ids) == 1 else "agent.sequence"
            ),
            agent_version_ids=selected_version_ids,
            required_capability_ids=selected_capabilities,
            step_conditions=step_conditions,
            parallel_groups=parallel_groups,
            step_dependencies=step_dependencies,
            input_schema=thaw_json_value(current_version.input_schema),
            output_schema=thaw_json_value(current_version.output_schema),
            configuration=thaw_json_value(current_version.configuration),
            created_at=now,
        )
        next_workflow = WorkflowDefinition.model_validate(
            workflow.model_copy(
                update={
                    "current_version_id": next_workflow_version.workflow_version_id,
                    "updated_at": now,
                }
            ).model_dump()
        )

        self._persist_replaced_workflow_agents(
            actor,
            changed_definitions,
            current_version,
            next_workflow,
            next_workflow_version,
            now,
            replacements,
            selected,
            selected_version_ids,
            workflow,
            workflow_publications,
            workflow.workflow_id,
        )
        return next_workflow

    def _prepare_replacement_publications(
        self,
        selected: list[tuple[AgentDefinition, AgentVersion]],
        workflow: WorkflowDefinition,
        now: Any,
    ) -> tuple[
        dict[str, AgentVersion],
        dict[str, AgentDefinition],
        dict[str, tuple[WorkflowDefinition, WorkflowVersion]],
    ]:
        """Prepare changed agents and dependent workflow version publications."""
        replacements: dict[str, AgentVersion] = {}
        changed: dict[str, AgentDefinition] = {}
        self._publish_replacement_agent_versions(
            changed, now, replacements, selected, workflow.workflow_id
        )
        new_versions = {
            version.agent_version_id: version for version in replacements.values()
        }
        linked: dict[str, tuple[WorkflowDefinition, WorkflowVersion]] = {}
        self._republish_workflows_for_replaced_agents(
            now, replacements, new_versions, workflow, linked, workflow.workflow_id
        )
        return replacements, changed, linked

    def _load_replacement_selection(
        self,
        workflow: WorkflowDefinition,
        agent_ids: tuple[str, ...],
        step_conditions: tuple[WorkflowStepCondition, ...],
        parallel_groups: tuple[WorkflowParallelGroup, ...],
        step_dependencies: tuple[WorkflowStepDependency, ...],
    ) -> tuple[WorkflowVersion, list[tuple[AgentDefinition, AgentVersion]]]:
        """Load agent versions and validate the proposed workflow graph."""
        if not 1 <= len(agent_ids) <= 10:
            raise ValueError("workflows require one to ten agent IDs")
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("workflow agent IDs must be unique")
        if workflow.current_version_id is None:
            raise ValueError("workflow has no published version")
        current = self.workflow_versions.get_scoped(
            workflow.current_version_id, workflow.org_id, workflow.workspace_id
        )
        selected = self._load_active_agents(workflow, agent_ids)
        indexes = {
            definition.id: index for index, (definition, _) in enumerate(selected)
        }
        self._validate_replacement_conditions(indexes, step_conditions)
        dependencies = {item.target_agent_id: item for item in step_dependencies}
        if len(dependencies) != len(step_dependencies):
            raise ValueError("workflow steps can have only one dependency policy")
        self._validate_replacement_dependencies(
            dependencies, indexes, agent_ids, step_conditions, step_dependencies
        )
        self._validate_parallel_groups(
            set(),
            indexes,
            agent_ids,
            parallel_groups,
            step_conditions,
            step_dependencies,
        )
        return current, selected

    def _load_active_agents(
        self, workflow: WorkflowDefinition, agent_ids: tuple[str, ...]
    ) -> list[tuple[AgentDefinition, AgentVersion]]:
        """Load active scoped agent definitions and current versions."""
        selected = []
        for agent_id in agent_ids:
            definition = self.agents.get_scoped(
                agent_id, workflow.org_id, workflow.workspace_id
            )
            if definition.status is not AgentStatus.ACTIVE:
                raise ValueError("all workflow agents must be active")
            version = self.agent_versions.get_scoped(
                definition.current_version_id or "",
                workflow.org_id,
                workflow.workspace_id,
            )
            selected.append((definition, version))
        return selected

    @staticmethod
    def _validate_replacement_conditions(
        indexes: dict[str, int], conditions: tuple[WorkflowStepCondition, ...]
    ) -> None:
        """Require condition sources to precede their target workflow steps."""
        for condition in conditions:
            source_index = indexes.get(condition.source_agent_id)
            target_index = indexes.get(condition.target_agent_id)
            if source_index is None or target_index is None:
                raise ValueError(
                    "condition source and target agents must be workflow steps"
                )
            if source_index >= target_index:
                raise ValueError(
                    "condition source step must come before its target step"
                )

    def _validate_replacement_dependencies(
        self,
        dependencies_by_target: Any,
        selected_indexes: Any,
        agent_ids: Any,
        step_conditions: Any,
        step_dependencies: Any,
    ) -> None:
        """Validate step dependencies for the replacement sequence."""
        if step_dependencies:
            if not set(dependencies_by_target).issubset(set(agent_ids[1:])):
                raise ValueError(
                    "graph dependencies can target only steps after the first root"
                )
            for dependency in step_dependencies:
                target_index = selected_indexes.get(dependency.target_agent_id)
                if target_index is None or target_index == 0:
                    raise ValueError(
                        "graph dependencies require a non-root target step"
                    )
                for source_id in dependency.depends_on_agent_ids:
                    source_index = selected_indexes.get(source_id)
                    if source_index is None or source_index >= target_index:
                        raise ValueError(
                            "workflow dependencies must reference earlier steps"
                        )
            dependency_sources_by_target = {
                item.target_agent_id: set(item.depends_on_agent_ids)
                for item in step_dependencies
            }
            if any(
                condition.source_agent_id
                not in dependency_sources_by_target.get(
                    condition.target_agent_id, set()
                )
                for condition in step_conditions
            ):
                raise ValueError(
                    "a condition source must be a declared step dependency"
                )

    def _republish_workflows_for_replaced_agents(
        self,
        now: Any,
        replacements: Any,
        replacements_by_new_id: Any,
        workflow: Any,
        workflow_publications: Any,
        workflow_id: Any,
    ) -> None:
        """Republish workflows that reference replaced agent versions."""
        for linked_id in {
            linked_id
            for replacement in replacements.values()
            for linked_id in replacement.allowed_workflow_ids
            if linked_id != workflow_id
        }:
            linked = self.workflows.get_scoped(
                linked_id, workflow.org_id, workflow.workspace_id
            )
            if linked.current_version_id is None:
                continue
            previous = self.workflow_versions.get_scoped(
                linked.current_version_id, workflow.org_id, workflow.workspace_id
            )
            rebound_ids = tuple(
                replacements[version_id].agent_version_id
                if version_id in replacements
                else version_id
                for version_id in previous.agent_version_ids
            )
            if rebound_ids == previous.agent_version_ids:
                continue
            capabilities = tuple(
                dict.fromkeys(
                    capability_id
                    for version_id in rebound_ids
                    for capability_id in (
                        replacements_by_new_id[version_id].allowed_capability_ids
                        if version_id in replacements_by_new_id
                        else self.agent_versions.get_scoped(
                            version_id, workflow.org_id, workflow.workspace_id
                        ).allowed_capability_ids
                    )
                )
            )
            next_version = WorkflowVersion(
                workflow_version_id=_id(),
                workflow_id=linked_id,
                org_id=workflow.org_id,
                workspace_id=workflow.workspace_id,
                version=previous.version + 1,
                handler_key=(
                    "agent.execute" if len(rebound_ids) == 1 else "agent.sequence"
                ),
                agent_version_ids=rebound_ids,
                required_capability_ids=capabilities,
                step_conditions=previous.step_conditions,
                parallel_groups=previous.parallel_groups,
                step_dependencies=previous.step_dependencies,
                input_schema=thaw_json_value(previous.input_schema),
                output_schema=thaw_json_value(previous.output_schema),
                configuration=thaw_json_value(previous.configuration),
                created_at=now,
            )
            next_definition = WorkflowDefinition.model_validate(
                linked.model_copy(
                    update={
                        "current_version_id": next_version.workflow_version_id,
                        "updated_at": now,
                    }
                ).model_dump()
            )
            workflow_publications[linked_id] = (next_definition, next_version)
