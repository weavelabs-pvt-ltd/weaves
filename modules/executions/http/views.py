"""Shared API response projections for runtime execution records."""

from typing import Any, cast

from modules.agents.execution_service import AgentExecutionService


def execution_run_view(
    execution_service: AgentExecutionService,
    run: Any,
) -> dict[str, Any]:
    """Serialize a run with its related agent, tool, and artifact history."""
    result = cast(dict[str, Any], run.model_dump(mode="json"))
    agent_runs, tool_invocations, artifacts = execution_service.run_projection_records(
        run
    )
    result["agent_runs"] = [item.model_dump(mode="json") for item in agent_runs]
    result["tool_invocations"] = [
        item.model_dump(mode="json") for item in tool_invocations
    ]
    result["artifacts"] = [item.model_dump(mode="json") for item in artifacts]
    return result
