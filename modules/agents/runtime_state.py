"""State shared by the steps of one agent run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event
from typing import Any

from contracts.v1 import Artifact, ConversationThread, ToolInvocation, WorkflowRun


@dataclass
class _AgentRunState:
    """Mutable state for one agent step, shared by the execution steps."""

    task: str
    requested_by_principal_id: str
    agent_id: str | None
    thread_id: str | None
    idempotency_key: str | None
    workflow_id: str | None
    agent_version_id: str | None
    pinned_workflow_version_id: str | None
    workflow_run: WorkflowRun | None
    finalize_run: bool
    workflow_step_index: int
    workflow_step_count: int
    previous_step_artifacts: tuple[Artifact, ...]
    cancellation_event: Event | None
    cancellation_check: Callable[[], bool] | None
    thread: ConversationThread | None = None
    definition: Any = None
    agent_version: Any = None
    workflow: Any = None
    workflow_version_id: str | None = None
    profile: Any = None
    provider: Any = None
    binding: Any = None
    now: datetime | None = None
    run_id: str = ""
    agent_run_id: str = ""
    artifact_id: str = ""
    execution_started: float = 0
    run: Any = None
    agent_run: Any = None
    contexts: list[dict[str, Any]] = field(default_factory=list)
    invocations: list[ToolInvocation] = field(default_factory=list)
    skipped_capability_ids: list[str] = field(default_factory=list)
    context_invocation_count: int = 0
    context: Any = ()
    context_text: str = ""
    history_messages: Any = ()
    omitted_context_source_ids: Any = ()
    prior_step_context: str = ""
    skipped_context_note: str = ""
    truncated_context_source_ids: Any = ()
    truncated_workflow_reference_ids: Any = ()
    model_response: Any = None
    estimated_cost_usd: Any = None
    metered_agent_run: Any = None
    response_text: str = ""
    structured_output: Any = None
    output_summary: dict[str, Any] = field(default_factory=dict)


class RunCostBudgetExceeded(RuntimeError):
    """A model completion exceeded its configured estimated-cost cap."""


class AgentRuntimeBudgetExceeded(RuntimeError):
    """An agent step exceeded its wall-clock budget at a safe boundary."""

    code = "agent.runtime_budget_exceeded"
    retryable = False
