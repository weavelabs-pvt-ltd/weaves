"""Execute the immutable agent sequence captured by a workflow version."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from contracts.v1 import (
    AgentDefinition,
    AgentVersion,
    Artifact,
    AuditEvent,
    AuditTargetScope,
    RunStatus,
    WorkflowJoinPolicy,
    WorkflowRun,
)
from modules.workflows.runtime_execution_entrypoint import WorkflowExecutionEntryMixin
from modules.workflows.runtime_execution_validation import (
    WorkflowExecutionPlan,
    WorkflowExecutionValidationMixin,
)
from modules.workflows.runtime_result import RuntimeResult
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import ExecutionCancelled
from shared.workflow_conditions import (
    workflow_condition_matches as _workflow_condition_matches,
)


@dataclass
class WorkflowExecutionProgress:
    """Mutable results accumulated while the workflow runs."""

    actor: str
    task: str
    thread_id: str | None
    idempotency_key: str | None
    cancellation_event: Event | None
    cancellation_check: Callable[[], bool] | None
    plan: WorkflowExecutionPlan
    results: list[RuntimeResult] = field(default_factory=list)
    results_by_agent: dict[str, RuntimeResult] = field(default_factory=dict)
    succeeded_by_agent: dict[str, bool] = field(default_factory=dict)
    skipped_steps: int = 0
    parent_run: WorkflowRun | None = None
    prior_artifacts: list[Artifact] = field(default_factory=list)
    cancellation_audit: AuditEvent | None = None


class WorkflowExecutionMixin(
    WorkflowExecutionEntryMixin, WorkflowExecutionValidationMixin
):
    """Run published workflows and build their aggregate execution results."""

    def _execute_workflow_sequence(
        self,
        workflow_id: str,
        task: str,
        actor: str,
        thread_id: str | None,
        idempotency_key: str | None,
        *,
        pinned_workflow_version_id: str | None = None,
        cancellation_event: Event | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Execute validated workflow steps, honoring dependencies and cancellation."""
        plan = self._load_workflow_execution_plan(
            workflow_id, thread_id, pinned_workflow_version_id
        )
        progress = WorkflowExecutionProgress(
            actor=actor,
            task=task,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            cancellation_event=cancellation_event,
            cancellation_check=cancellation_check,
            plan=plan,
        )
        self._execute_workflow_steps(progress)
        return self._complete_workflow_execution(progress)

    def _execute_workflow_steps(self, progress: WorkflowExecutionProgress) -> None:
        """Run each ordered step and update aggregate progress."""
        plan = progress.plan
        for index, pair in enumerate(
            zip(plan.agent_versions, plan.agents, strict=True)
        ):
            agent_version, agent = pair
            if self._is_workflow_cancelled(progress):
                self._cancel_workflow_execution(progress)
                return
            self._execute_workflow_step(progress, index, agent, agent_version)

    @staticmethod
    def _is_workflow_cancelled(progress: WorkflowExecutionProgress) -> bool:
        """Check both local and durable cancellation signals."""
        return (
            progress.cancellation_event is not None
            and progress.cancellation_event.is_set()
        ) or (progress.cancellation_check is not None and progress.cancellation_check())

    def _cancel_workflow_execution(self, progress: WorkflowExecutionProgress) -> None:
        """Persist a safe-boundary cancellation after at least one step ran."""
        parent = progress.parent_run
        if parent is None:
            raise ExecutionCancelled(
                "Execution cancellation was requested before the workflow started"
            )
        finished = _now()
        cancelled = WorkflowRun.model_validate(
            parent.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "finished_at": finished,
                    "updated_at": finished,
                }
            ).model_dump()
        )
        audit = AuditEvent(
            audit_event_id=_id(),
            org_id=parent.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=parent.workspace_id,
            actor_principal_id=progress.actor,
            action="workflow.run.cancelled",
            target_type="workflow_run",
            target_id=parent.run_id,
            summary="Workflow run cancelled between steps",
            request_id=parent.run_id,
            metadata={"completed_steps": len(progress.results)},
            created_at=finished,
        )
        with self._unit_of_work():
            self.runs.put(cancelled)
            self.audit_events.create(audit)
        progress.parent_run = cancelled
        progress.cancellation_audit = audit

    def _execute_workflow_step(
        self,
        progress: WorkflowExecutionProgress,
        index: int,
        agent: AgentDefinition,
        version: AgentVersion,
    ) -> None:
        """Run a parallel group or one enabled sequential step."""
        plan = progress.plan
        if index in plan.groups_by_start:
            self._execute_parallel_group(progress, plan.groups_by_start[index])
            return
        if index in plan.group_members or not self._workflow_step_enabled(
            progress, index
        ):
            return
        result = self._execute_single_workflow_step(
            progress, index, agent, version, parallel=False
        )
        self._record_workflow_result(progress, index, result)

    def _execute_parallel_group(
        self, progress: WorkflowExecutionProgress, indexes: tuple[int, ...]
    ) -> None:
        """Execute enabled members of one published parallel group."""
        runnable = tuple(
            index for index in indexes if self._workflow_step_enabled(progress, index)
        )
        if not runnable:
            return
        with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
            futures = {
                index: pool.submit(
                    copy_context().run,
                    self._execute_single_workflow_step,
                    progress,
                    index,
                    progress.plan.agents[index],
                    progress.plan.agent_versions[index],
                    True,
                )
                for index in runnable
            }
            results = tuple(futures[index].result() for index in runnable)
        for index, result in zip(runnable, results, strict=True):
            self._record_workflow_result(progress, index, result)

    def _execute_single_workflow_step(
        self,
        progress: WorkflowExecutionProgress,
        index: int,
        agent: AgentDefinition,
        version: AgentVersion,
        parallel: bool,
    ) -> RuntimeResult:
        """Invoke one agent with the workflow's pinned version and context."""
        plan = progress.plan
        return self._execute_agent_run(
            progress.task,
            progress.actor,
            agent.id,
            progress.thread_id if index == 0 and not parallel else None,
            idempotency_key=(
                progress.idempotency_key if index == 0 and not parallel else None
            ),
            workflow_id=plan.workflow.workflow_id,
            agent_version_id=version.agent_version_id,
            pinned_workflow_version_id=plan.version.workflow_version_id,
            workflow_run=progress.parent_run,
            finalize_run=(not parallel and index == len(plan.agent_versions) - 1),
            workflow_step_index=index,
            workflow_step_count=len(plan.agent_versions),
            previous_step_artifacts=self._workflow_step_artifacts(progress, index),
            cancellation_event=None if parallel else progress.cancellation_event,
            cancellation_check=None if parallel else progress.cancellation_check,
        )

    def _record_workflow_result(
        self, progress: WorkflowExecutionProgress, index: int, result: RuntimeResult
    ) -> None:
        """Update the shared execution state after a successful step."""
        agent_id = progress.plan.agents[index].id
        progress.parent_run = result.run
        progress.results.append(result)
        progress.results_by_agent[agent_id] = result
        progress.succeeded_by_agent[agent_id] = True
        progress.prior_artifacts.append(result.artifact)

    def _workflow_step_enabled(
        self, progress: WorkflowExecutionProgress, index: int
    ) -> bool:
        """Evaluate dependencies and output conditions for one step."""
        plan = progress.plan
        agent_id = plan.agents[index].id
        dependency = plan.dependencies_by_target.get(agent_id)
        if dependency is not None and not self._workflow_dependencies_met(
            progress, index, dependency
        ):
            return False
        condition = plan.conditions_by_target.get(agent_id)
        if condition is None:
            return True
        source = progress.results_by_agent.get(condition.source_agent_id)
        summary = source.agent_run.output_summary or {} if source else {}
        structured = summary.get("structured_output")
        matches, reason = _workflow_condition_matches(
            condition,
            structured,
            has_structured_output="structured_output" in summary,
        )
        if matches:
            return True
        self._record_skipped_workflow_step(
            progress,
            index,
            reason,
            {
                "source_agent_id": condition.source_agent_id,
                "operator": condition.operator.value,
            },
        )
        return False

    def _workflow_dependencies_met(
        self, progress: WorkflowExecutionProgress, index: int, dependency: Any
    ) -> bool:
        """Evaluate ALL or ANY dependency policy for one step."""
        outcomes = tuple(
            progress.succeeded_by_agent.get(source_id, False)
            for source_id in dependency.depends_on_agent_ids
        )
        met = (
            all(outcomes)
            if dependency.join_policy is WorkflowJoinPolicy.ALL
            else any(outcomes)
        )
        if not met:
            self._record_skipped_workflow_step(
                progress,
                index,
                "dependencies_not_satisfied",
                {
                    "depends_on_agent_ids": list(dependency.depends_on_agent_ids),
                    "join_policy": dependency.join_policy.value,
                },
            )
        return met

    def _record_skipped_workflow_step(
        self,
        progress: WorkflowExecutionProgress,
        index: int,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        """Record a skipped step without including sensitive output values."""
        plan = progress.plan
        agent = plan.agents[index]
        progress.skipped_steps += 1
        progress.succeeded_by_agent[agent.id] = False
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=plan.workflow.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=plan.workflow.workspace_id,
                actor_principal_id=progress.actor,
                action="workflow.step.skipped",
                target_type="workflow_step",
                target_id=agent.id,
                summary="Workflow step skipped by its published execution rules",
                request_id=(
                    progress.parent_run.run_id
                    if progress.parent_run
                    else plan.workflow.workflow_id
                ),
                metadata={
                    "workflow_id": plan.workflow.workflow_id,
                    "workflow_version_id": plan.version.workflow_version_id,
                    "target_agent_id": agent.id,
                    "step_index": index,
                    "reason": reason,
                    **details,
                },
                created_at=_now(),
            )
        )

    @staticmethod
    def _workflow_step_artifacts(
        progress: WorkflowExecutionProgress, index: int
    ) -> tuple[Artifact, ...]:
        """Select only successful dependency artifacts for a workflow step."""
        plan = progress.plan
        agent_id = plan.agents[index].id
        dependency = plan.dependencies_by_target.get(agent_id)
        if dependency is None:
            return (
                ()
                if plan.version.step_dependencies
                else tuple(progress.prior_artifacts)
            )
        return tuple(
            progress.results_by_agent[source_id].artifact
            for source_id in dependency.depends_on_agent_ids
            if progress.succeeded_by_agent.get(source_id, False)
            and source_id in progress.results_by_agent
        )

    def _complete_workflow_execution(
        self, progress: WorkflowExecutionProgress
    ) -> RuntimeResult:
        """Finalize an aggregate workflow run and construct its result."""
        if progress.cancellation_audit is not None:
            if progress.parent_run is None:
                raise RuntimeError("cancelled workflow is missing its run record")
            return self._aggregate_workflow_result(
                progress, progress.parent_run, progress.cancellation_audit
            )
        final = progress.results[-1]
        parent = progress.parent_run
        if parent is None or parent.status is not RunStatus.RUNNING:
            return self._aggregate_workflow_result(
                progress, final.run, final.audit_event
            )
        finished = _now()
        completed = WorkflowRun.model_validate(
            parent.model_copy(
                update={
                    "status": RunStatus.SUCCEEDED,
                    "finished_at": finished,
                    "updated_at": finished,
                }
            ).model_dump()
        )
        audit = self._workflow_completion_audit(progress, completed, finished)
        with self._unit_of_work():
            self.runs.put(completed)
            self.audit_events.create(audit)
        return self._aggregate_workflow_result(progress, completed, audit)

    @staticmethod
    def _workflow_completion_audit(
        progress: WorkflowExecutionProgress, run: WorkflowRun, finished: Any
    ) -> AuditEvent:
        """Build the final workflow audit event with execution counts."""
        plan = progress.plan
        return AuditEvent(
            audit_event_id=_id(),
            org_id=plan.workflow.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=plan.workflow.workspace_id,
            actor_principal_id=progress.actor,
            action="workflow.run.succeeded",
            target_type="workflow_run",
            target_id=run.run_id,
            summary="Workflow completed",
            request_id=run.run_id,
            metadata={
                "workflow_version_id": plan.version.workflow_version_id,
                "executed_steps": len(progress.results),
                "skipped_steps": progress.skipped_steps,
            },
            created_at=finished,
        )

    @staticmethod
    def _aggregate_workflow_result(
        progress: WorkflowExecutionProgress,
        run: WorkflowRun,
        audit: AuditEvent,
    ) -> RuntimeResult:
        """Collect step-level outputs under the final workflow result."""
        final = progress.results[-1]
        return RuntimeResult(
            run=run,
            agent_run=final.agent_run,
            artifact=final.artifact,
            tool_invocations=tuple(
                invocation
                for result in progress.results
                for invocation in result.tool_invocations
            ),
            audit_event=audit,
            workflow_agent_runs=tuple(result.agent_run for result in progress.results),
            workflow_artifacts=tuple(result.artifact for result in progress.results),
        )
