"""Result contract returned by synchronous workflow execution."""

from __future__ import annotations

from dataclasses import dataclass

from contracts.v1 import AgentRun, Artifact, AuditEvent, ToolInvocation, WorkflowRun


@dataclass(frozen=True)
class RuntimeResult:
    """Aggregate run, agent, artifact, audit, and tool invocation results."""

    run: WorkflowRun
    agent_run: AgentRun
    artifact: Artifact
    tool_invocations: tuple[ToolInvocation, ...]
    audit_event: AuditEvent
    replayed: bool = False
    workflow_agent_runs: tuple[AgentRun, ...] = ()
    workflow_artifacts: tuple[Artifact, ...] = ()

    @property
    def tool_invocation(self) -> ToolInvocation | None:
        """Compatibility accessor for the first invoked capability."""
        return self.tool_invocations[0] if self.tool_invocations else None
