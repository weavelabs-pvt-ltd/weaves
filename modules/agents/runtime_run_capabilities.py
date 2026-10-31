"""Invoke and audit contextual tools used by agent runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentRun,
    AgentVersion,
    CapabilityKind,
    CapabilitySpec,
    ErrorSummary,
    InvocationStatus,
    PluginInstallation,
    RiskLevel,
    ToolInvocation,
    WorkflowRun,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import PluginGatewayError
from shared.request_context import effective_org_id, effective_workspace_id


class AgentRunCapabilitiesMixin(RuntimeCapabilityHost):
    """Execute approved read capabilities and persist invocation outcomes."""

    def _collect_agent_capability_context(
        self,
        agent_run: Any,
        agent_run_id: Any,
        agent_version: Any,
        cancel_if_requested: Any,
        context_invocation_count: Any,
        contexts: Any,
        execution_started: Any,
        invocations: Any,
        requested_by_principal_id: Any,
        run: Any,
        run_id: Any,
        skipped_capability_ids: Any,
        task: Any,
    ) -> tuple[Any, ...]:
        """Invoke approved context capabilities for one agent run."""
        for capability_id in agent_version.allowed_capability_ids:
            cancel_if_requested()
            try:
                installation, capability = (
                    self.integration_admin_service.resolve_capability_installation(
                        capability_id,
                        org_id=effective_org_id(),
                        workspace_id=effective_workspace_id(),
                        require_enabled=False,
                        allowed_kinds=(
                            CapabilityKind.CONTEXT_READ,
                            CapabilityKind.ACTION_WRITE,
                        ),
                    )
                )
            except ValueError as exc:
                self._record_run_failure(
                    run,
                    agent_run,
                    requested_by_principal_id,
                    "plugin.capability_unavailable",
                    str(exc),
                    retryable=False,
                )
                raise
            if capability.kind is CapabilityKind.ACTION_WRITE:
                # Write actions are proposed explicitly and run only after approval.
                continue
            if context_invocation_count >= agent_version.budget.max_tool_calls:
                skipped_capability_ids.append(capability_id)
                continue
            context_invocation_count += 1
            run, invocation, documents = self._invoke_agent_context_capability(
                capability_id,
                installation,
                capability,
                agent_run,
                agent_run_id,
                agent_version,
                execution_started,
                context_invocation_count,
                invocations,
                requested_by_principal_id,
                run,
                run_id,
                task,
            )
            invocations.append(invocation)
            contexts.extend(
                document for document in documents if isinstance(document, dict)
            )
        return context_invocation_count, run

    def _invoke_agent_context_capability(
        self,
        capability_id: str,
        installation: PluginInstallation,
        capability: CapabilitySpec,
        agent_run: AgentRun,
        agent_run_id: str,
        agent_version: AgentVersion,
        execution_started: float,
        context_invocation_count: int,
        invocations: list[ToolInvocation],
        actor: str,
        run: WorkflowRun,
        run_id: str,
        task: str,
    ) -> tuple[WorkflowRun, ToolInvocation, list[Any]]:
        """Enforce budgets, invoke a context tool, and persist its outcome."""
        self._enforce_agent_runtime_budget(
            execution_started,
            agent_version.budget,
            run,
            agent_run,
            actor,
            audit_metadata={"context_calls_made": context_invocation_count - 1},
        )
        run = self._touch_run_heartbeat(run)
        started = _now()
        try:
            result = self.plugin_gateway.invoke(
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                installation_id=installation.plugin_installation_id,
                agent_version=agent_version,
                capability_id=capability_id,
                payload={"query": task},
            )
        except PluginGatewayError as exc:
            self._persist_failed_context_invocation(
                capability_id,
                installation,
                capability,
                agent_run,
                agent_run_id,
                invocations,
                actor,
                run,
                run_id,
                task,
                started,
                exc,
            )
            raise
        documents = result.output.get("documents", [])
        invocation = ToolInvocation(
            invocation_id=_id(),
            run_id=run_id,
            agent_run_id=agent_run_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_installation_id=installation.plugin_installation_id,
            capability_id=capability_id,
            status=InvocationStatus.SUCCEEDED,
            risk_level=capability.risk_level or RiskLevel.LOW,
            input_summary={"query": task},
            output_summary={"result_count": len(documents)},
            created_at=started,
            updated_at=result.finished_at,
            started_at=result.started_at,
            finished_at=result.finished_at,
            duration_ms=result.duration_ms,
        )
        with self._unit_of_work():
            self.tool_invocations.create(invocation)
            self._audit_capability_invocation(invocation, installation.plugin_id, actor)
        return run, invocation, documents if isinstance(documents, list) else []

    def _persist_failed_context_invocation(
        self,
        capability_id: str,
        installation: PluginInstallation,
        capability: CapabilitySpec,
        agent_run: AgentRun,
        agent_run_id: str,
        invocations: list[ToolInvocation],
        actor: str,
        run: WorkflowRun,
        run_id: str,
        task: str,
        started: datetime,
        error: PluginGatewayError,
    ) -> None:
        """Store a failed context invocation and mark its run as failed."""
        finished = _now()
        failed = ToolInvocation(
            invocation_id=_id(),
            run_id=run_id,
            agent_run_id=agent_run_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            plugin_installation_id=installation.plugin_installation_id,
            capability_id=capability_id,
            status=InvocationStatus.FAILED,
            risk_level=capability.risk_level or RiskLevel.LOW,
            input_summary={"query": task},
            created_at=started,
            updated_at=finished,
            started_at=started,
            finished_at=finished,
            duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
            error=ErrorSummary(code=error.code, summary=error.message),
        )
        with self._unit_of_work():
            self.tool_invocations.create(failed)
            invocations.append(failed)
            self._audit_capability_invocation(failed, installation.plugin_id, actor)
            self._record_run_failure(
                run, agent_run, actor, error.code, error.message, retryable=False
            )
