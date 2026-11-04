"""Build stable graph projections for workflow definition and execution UIs."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from contracts.v1 import AgentDefinition, AgentVersion, WorkflowVersion


def workflow_graph_view(
    version: WorkflowVersion,
    agent_versions: tuple[AgentVersion, ...],
    agents_by_id: dict[str, AgentDefinition],
) -> dict[str, Any]:
    """Project a pinned workflow version into graph nodes, stages, and edges."""
    versions_by_id = {item.agent_version_id: item for item in agent_versions}
    parallel_group_by_agent = {
        agent_id: f"parallel-{group_index + 1}"
        for group_index, group in enumerate(version.parallel_groups)
        for agent_id in group.agent_ids
    }
    conditions_by_target = {
        item.target_agent_id: item for item in version.step_conditions
    }
    dependencies_by_target = {
        item.target_agent_id: item for item in version.step_dependencies
    }
    nodes: list[dict[str, Any]] = []
    node_ids_by_agent: dict[str, str] = {}
    for step_index, version_id in enumerate(version.agent_version_ids):
        agent_version = versions_by_id[version_id]
        agent = agents_by_id[agent_version.agent_id]
        node_id = agent.id
        node_ids_by_agent[agent.id] = node_id
        nodes.append(
            {
                "node_id": node_id,
                "node_type": "agent",
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_version_id": agent_version.agent_version_id,
                "agent_version": agent_version.version,
                "step_index": step_index,
                "parallel_group_id": parallel_group_by_agent.get(agent.id),
                "condition": _dump_optional(conditions_by_target.get(agent.id)),
                "dependencies": _dump_optional(dependencies_by_target.get(agent.id)),
            }
        )
    stages = _workflow_stages(
        version, node_ids_by_agent, parallel_group_by_agent, versions_by_id
    )
    edges = _workflow_edges(version, stages, node_ids_by_agent)
    return {
        "workflow_id": version.workflow_id,
        "workflow_version_id": version.workflow_version_id,
        "version": version.version,
        "handler_key": version.handler_key,
        "stages": stages,
        "nodes": nodes,
        "edges": edges,
    }


def _dump_optional(value: Any) -> dict[str, Any] | None:
    """Serialize an optional Pydantic condition or dependency contract."""
    return value.model_dump(mode="json") if value is not None else None


def _workflow_stages(
    version: WorkflowVersion,
    node_ids_by_agent: dict[str, str],
    parallel_group_by_agent: dict[str, str],
    versions_by_id: dict[str, AgentVersion],
) -> list[dict[str, Any]]:
    """Group ordered steps into single-agent and parallel execution stages."""
    groups_by_id = {
        f"parallel-{index + 1}": group.agent_ids
        for index, group in enumerate(version.parallel_groups)
    }
    stages: list[dict[str, Any]] = []
    consumed_groups: set[str] = set()
    for agent_version_id in version.agent_version_ids:
        agent_id = versions_by_id[agent_version_id].agent_id
        group_id = parallel_group_by_agent.get(agent_id)
        if group_id is not None and group_id in consumed_groups:
            continue
        agent_ids = groups_by_id[group_id] if group_id else (agent_id,)
        if group_id is not None:
            consumed_groups.add(group_id)
        index = len(stages)
        stages.append(
            {
                "stage_id": f"stage-{index + 1}",
                "stage_index": index,
                "execution_mode": "parallel" if group_id else "sequential",
                "parallel_group_id": group_id,
                "node_ids": [node_ids_by_agent[item] for item in agent_ids],
            }
        )
    return stages


def _workflow_edges(
    version: WorkflowVersion,
    stages: list[dict[str, Any]],
    node_ids_by_agent: dict[str, str],
) -> list[dict[str, Any]]:
    """Create execution links and annotate explicit conditions and dependencies."""
    edges_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for previous, following in pairwise(stages):
        for source in previous["node_ids"]:
            for target in following["node_ids"]:
                edges_by_pair[(source, target)] = _new_edge(source, target, "flow")
    for condition in version.step_conditions:
        source = node_ids_by_agent[condition.source_agent_id]
        target = node_ids_by_agent[condition.target_agent_id]
        edge = edges_by_pair.setdefault(
            (source, target), _new_edge(source, target, "condition")
        )
        edge["condition"] = condition.model_dump(mode="json")
    for dependency in version.step_dependencies:
        target = node_ids_by_agent[dependency.target_agent_id]
        for source_agent_id in dependency.depends_on_agent_ids:
            source = node_ids_by_agent[source_agent_id]
            edge = edges_by_pair.setdefault(
                (source, target), _new_edge(source, target, "dependency")
            )
            edge["dependency"] = {
                "join_policy": dependency.join_policy.value,
                "depends_on_agent_ids": list(dependency.depends_on_agent_ids),
            }
    return list(edges_by_pair.values())


def _new_edge(source: str, target: str, edge_type: str) -> dict[str, Any]:
    """Create a stable graph edge record for a source and target node pair."""
    return {
        "edge_id": f"{source}->{target}",
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": edge_type,
        "condition": None,
        "dependency": None,
    }
