"""Persist agent execution failures, cancellations, and capability audits."""

from __future__ import annotations

from time import monotonic
from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentBudget,
    AgentRun,
    AuditEvent,
    AuditTargetScope,
    ErrorSummary,
    InvocationStatus,
    RunStatus,
    ToolInvocation,
    WorkflowRun,
)
from modules.agents.runtime_state import AgentRuntimeBudgetExceeded
from shared.clock import new_id as _id
from shared.clock import utc_now as _now


class AgentRunErrorMixin(RuntimeCapabilityHost):
    """Store terminal agent outcomes while preserving exact audits."""

    def _enforce_agent_runtime_budget(
        self,
        execution_started: float,
        budget: AgentBudget,
        run: WorkflowRun,
        agent_run: AgentRun,
        actor_principal_id: str,
        *,
        audit_metadata: dict[str, Any] | None = None,
    ) -> None:
        """Fail an agent step at a safe boundary after its wall-clock budget."""
        elapsed_seconds = monotonic() - execution_started
        if elapsed_seconds < budget.max_runtime_seconds:
            return
        message = (
            f"Agent runtime exceeded its {budget.max_runtime_seconds}-second "
            "budget at a safe boundary"
        )
        self._record_run_failure(
            run,
            agent_run,
            actor_principal_id,
            "agent.runtime_budget_exceeded",
            message,
            retryable=False,
            audit_metadata={
                "runtime_seconds": round(elapsed_seconds, 3),
                "runtime_limit_seconds": budget.max_runtime_seconds,
                **(audit_metadata or {}),
            },
        )
        raise AgentRuntimeBudgetExceeded(message)

    def _record_run_failure(
        self,
        run: WorkflowRun,
        agent_run: AgentRun,
        actor_principal_id: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        audit_metadata: dict[str, Any] | None = None,
    ) -> None:
        finished = _now()
        summary = ErrorSummary(
            code=error_code, summary=error_message, retryable=retryable
        )
        failed_agent_run = agent_run.model_copy(
            update={
                "status": RunStatus.FAILED,
                "error": summary,
                "finished_at": finished,
                "updated_at": finished,
            }
        )
        failed_run = run.model_copy(
            update={
                "status": RunStatus.FAILED,
                "error": summary,
                "finished_at": finished,
                "updated_at": finished,
            }
        )
        with self._unit_of_work():
            self.agent_runs.put(failed_agent_run)
            self.runs.put(failed_run)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=run.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=run.workspace_id,
                    actor_principal_id=actor_principal_id,
                    action="agent.run.failed",
                    target_type="workflow_run",
                    target_id=run.run_id,
                    summary=error_message,
                    request_id=run.run_id,
                    metadata={"error_code": error_code, **(audit_metadata or {})},
                    created_at=finished,
                )
            )

    def _record_run_cancellation(
        self,
        run: WorkflowRun,
        agent_run: AgentRun,
        actor_principal_id: str,
        *,
        boundary: str,
    ) -> None:
        """Persist cooperative cancellation after the active call has returned."""
        finished = _now()
        cancelled_agent_run = AgentRun.model_validate(
            agent_run.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "finished_at": finished,
                    "updated_at": finished,
                }
            ).model_dump()
        )
        cancelled_run = WorkflowRun.model_validate(
            run.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "finished_at": finished,
                    "updated_at": finished,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.agent_runs.put(cancelled_agent_run)
            self.runs.put(cancelled_run)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=run.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=run.workspace_id,
                    actor_principal_id=actor_principal_id,
                    action="agent.run.cancelled",
                    target_type="workflow_run",
                    target_id=run.run_id,
                    summary="Agent run cancelled at a safe boundary",
                    request_id=run.run_id,
                    metadata={"boundary": boundary},
                    created_at=finished,
                )
            )

    def _audit_capability_invocation(
        self,
        invocation: ToolInvocation,
        plugin_id: str,
        actor_principal_id: str,
    ) -> None:
        status = (
            "succeeded" if invocation.status is InvocationStatus.SUCCEEDED else "failed"
        )
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=invocation.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=invocation.workspace_id,
                actor_principal_id=actor_principal_id,
                action=f"plugin.capability.{status}",
                target_type="tool_invocation",
                target_id=invocation.invocation_id,
                summary=f"{plugin_id} capability {status}",
                request_id=invocation.run_id,
                metadata={
                    "plugin_id": plugin_id,
                    "capability_id": invocation.capability_id,
                    "status": invocation.status.value,
                },
                created_at=invocation.updated_at,
            )
        )
