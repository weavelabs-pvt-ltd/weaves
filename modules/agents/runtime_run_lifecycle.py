"""Agent-run persistence and orchestration lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Event
from time import monotonic

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    Artifact,
    ArtifactSensitivity,
    ArtifactStatus,
    ConversationThread,
    Permission,
    RetentionClass,
    RunStatus,
    WorkflowRun,
)
from modules.agents.runtime_state import _AgentRunState as AgentRunState
from modules.workflows.runtime_result import RuntimeResult
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import ExecutionCancelled
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class AgentRunLifecycleMixin(RuntimeCapabilityHost):
    """Coordinate the phases of one agent run."""

    def _execute_agent_run(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        workflow_id: str | None = None,
        agent_version_id: str | None = None,
        pinned_workflow_version_id: str | None = None,
        workflow_run: WorkflowRun | None = None,
        finalize_run: bool = True,
        workflow_step_index: int = 0,
        workflow_step_count: int = 1,
        previous_step_artifacts: tuple[Artifact, ...] = (),
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Execute one agent step, optionally inside an existing workflow run."""
        state = AgentRunState(
            task=task,
            requested_by_principal_id=requested_by_principal_id,
            agent_id=agent_id,
            thread_id=thread_id,
            idempotency_key=idempotency_key,
            workflow_id=workflow_id,
            agent_version_id=agent_version_id,
            pinned_workflow_version_id=pinned_workflow_version_id,
            workflow_run=workflow_run,
            finalize_run=finalize_run,
            workflow_step_index=workflow_step_index,
            workflow_step_count=workflow_step_count,
            previous_step_artifacts=previous_step_artifacts,
            cancellation_event=_cancellation_event,
            cancellation_check=_cancellation_check,
        )
        self._prepare_agent_run_records(state)
        self._collect_agent_run_model_inputs(state)
        self._invoke_agent_model(state)
        return self._store_completed_agent_run(state)

    def _prepare_agent_run_records(self, state: AgentRunState) -> None:
        """Authorize the step and persist the running workflow and agent rows."""
        state.requested_by_principal_id = effective_principal_id(
            state.requested_by_principal_id
        )
        if not self.is_bootstrapped():
            self.bootstrap()
        state.task = state.task.strip()
        if not state.task:
            raise ValueError("task must not be empty")
        if len(state.task) > 12_000:
            raise ValueError("task must not exceed 12000 characters")
        state.requested_by_principal_id = effective_principal_id(
            state.requested_by_principal_id
        )
        self.authorize(state.requested_by_principal_id, Permission.AGENTS_RUN)
        if state.workflow_id is not None:
            self.authorize(state.requested_by_principal_id, Permission.WORKFLOWS_RUN)
        (
            state.thread,
            state.definition,
            state.agent_version,
            state.workflow,
            state.workflow_version_id,
            state.profile,
            state.provider,
            state.binding,
        ) = self._load_agent_execution_target(
            state.agent_id,
            state.agent_version_id,
            state.pinned_workflow_version_id,
            state.thread_id,
            state.workflow_id,
        )
        now = _now()
        state.now = now
        state.run_id, state.agent_run_id, state.artifact_id = (_id() for _ in range(3))
        state.execution_started = monotonic()
        state.run = self._running_workflow_run(state, now)
        state.run_id = state.run.run_id
        if state.thread is not None:
            updated_thread = state.thread.model_copy(update={"updated_at": now})
            state.thread = ConversationThread.model_validate(
                updated_thread.model_dump()
            )
        state.agent_run = self._build_agent_run_record(
            state.agent_run_id,
            state.agent_version,
            state.definition,
            now,
            state.profile,
            state.provider,
            state.run_id,
            state.task,
            state.previous_step_artifacts,
            state.thread_id,
            state.workflow_step_count,
            state.workflow_step_index,
        )
        with self._unit_of_work():
            if state.thread is not None:
                self.threads.put(state.thread)
            if state.workflow_run is None:
                self.runs.create(state.run)
            self.agent_runs.create(state.agent_run)

    def _running_workflow_run(self, state: AgentRunState, now: datetime) -> WorkflowRun:
        """Create a running parent row or reuse the workflow run for this step."""
        if state.workflow_run is None:
            if state.workflow_version_id is None:
                raise ValueError("workflow has no published version")
            return WorkflowRun(
                run_id=state.run_id,
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                workflow_id=state.workflow.workflow_id,
                workflow_version_id=state.workflow_version_id,
                requested_by_principal_id=state.requested_by_principal_id,
                trigger_type=(
                    "conversation.thread" if state.thread else "local.runtime"
                ),
                trigger_ref=state.thread_id,
                idempotency_key=state.idempotency_key,
                status=RunStatus.RUNNING,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
        if state.workflow_run.workflow_id != state.workflow.workflow_id:
            raise ValueError("workflow step does not match its parent run")
        if state.workflow_run.status is not RunStatus.RUNNING:
            raise ValueError("workflow parent run must still be running")
        return state.workflow_run

    def _agent_response_artifact(
        self, state: AgentRunState, finished: datetime
    ) -> Artifact:
        """Build the response artifact for a completed agent step."""
        return Artifact(
            artifact_id=state.artifact_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            run_id=state.run_id,
            agent_run_id=state.agent_run_id,
            artifact_type="agent.response",
            title="Agent response",
            summary=state.response_text,
            content_ref=f"memory://{state.artifact_id}",
            content_type="application.json",
            provenance=self._agent_run_provenance(
                state.agent_run_id,
                state.context,
                finished,
                state.previous_step_artifacts,
            ),
            sensitivity=ArtifactSensitivity.INTERNAL,
            retention_class=RetentionClass.STANDARD,
            status=ArtifactStatus.AVAILABLE,
            created_by_agent_run_id=state.agent_run_id,
            created_at=finished,
            updated_at=finished,
        )

    def _cancel_agent_run_if_requested(self, state: AgentRunState) -> None:
        """Stop the step before an external call when cancellation was requested."""
        requested = (
            state.cancellation_event is not None and state.cancellation_event.is_set()
        ) or (state.cancellation_check is not None and state.cancellation_check())
        if requested:
            self._record_run_cancellation(
                state.run,
                state.agent_run,
                state.requested_by_principal_id,
                boundary="before_external_call",
            )
            raise ExecutionCancelled("Execution cancellation was requested")

    def _collect_agent_run_model_inputs(self, state: AgentRunState) -> None:
        """Gather memory and capability context, then bound it for the model."""
        state.contexts = list(
            self.memory_context.build(
                items=self.memory_items.list_scoped(effective_org_id()),
                policy=state.agent_version.memory_policy,
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                query=state.task,
                workflow_id=state.workflow.workflow_id,
                thread_id=state.thread_id,
            )
        )
        (
            state.context_invocation_count,
            state.run,
        ) = self._collect_agent_capability_context(
            state.agent_run,
            state.agent_run_id,
            state.agent_version,
            lambda: self._cancel_agent_run_if_requested(state),
            state.context_invocation_count,
            state.contexts,
            state.execution_started,
            state.invocations,
            state.requested_by_principal_id,
            state.run,
            state.run_id,
            state.skipped_capability_ids,
            state.task,
        )
        (
            state.context,
            state.context_text,
            state.history_messages,
            state.omitted_context_source_ids,
            state.prior_step_context,
            state.skipped_context_note,
            state.truncated_context_source_ids,
            state.truncated_workflow_reference_ids,
        ) = self._assemble_agent_model_inputs(
            state.contexts,
            state.invocations,
            state.previous_step_artifacts,
            state.skipped_capability_ids,
            state.thread_id,
        )
        self._enforce_agent_runtime_budget(
            state.execution_started,
            state.agent_version.budget,
            state.run,
            state.agent_run,
            state.requested_by_principal_id,
            audit_metadata={"context_calls_made": state.context_invocation_count},
        )

    def _invoke_agent_model(self, state: AgentRunState) -> None:
        """Call the model and turn its response into the run output summary."""
        self._cancel_agent_run_if_requested(state)
        state.run = self._touch_run_heartbeat(state.run)
        state.model_response = self._request_agent_model_completion(
            state.agent_run,
            state.agent_version,
            state.binding,
            state.context,
            state.context_text,
            state.history_messages,
            state.prior_step_context,
            state.profile,
            state.requested_by_principal_id,
            state.run,
            state.run_id,
            state.skipped_context_note,
            state.task,
        )
        state.estimated_cost_usd, state.metered_agent_run = (
            self._apply_agent_model_cost_budget(
                state.agent_run,
                state.agent_version,
                state.execution_started,
                state.model_response,
                state.profile,
                state.requested_by_principal_id,
                state.run,
            )
        )
        state.response_text = state.model_response.content
        output_warnings: list[str] = []
        state.response_text, state.structured_output = (
            self._parse_structured_agent_output(
                state.agent_version,
                state.context,
                state.estimated_cost_usd,
                state.metered_agent_run,
                state.model_response,
                state.requested_by_principal_id,
                state.response_text,
                state.run,
            )
        )
        state.response_text, state.output_summary = self._agent_output_summary(
            state.agent_version,
            state.context,
            state.omitted_context_source_ids,
            output_warnings,
            state.response_text,
            state.skipped_capability_ids,
            state.structured_output,
            state.truncated_context_source_ids,
            state.truncated_workflow_reference_ids,
        )

    def _store_completed_agent_run(self, state: AgentRunState) -> RuntimeResult:
        """Persist the successful step, its artifact, and its audit event."""
        finished = _now()
        state.agent_run = state.agent_run.model_copy(
            update={
                "status": RunStatus.SUCCEEDED,
                "output_summary": state.output_summary,
                "resolved_provider_id": state.model_response.provider_id,
                "resolved_model_id": state.model_response.model_id,
                "input_tokens": state.model_response.usage.input_tokens,
                "output_tokens": state.model_response.usage.output_tokens,
                "estimated_cost_usd": state.estimated_cost_usd,
                "finished_at": finished,
                "updated_at": finished,
            }
        )
        if state.finalize_run:
            state.run = WorkflowRun.model_validate(
                state.run.model_copy(
                    update={
                        "status": RunStatus.SUCCEEDED,
                        "finished_at": finished,
                        "updated_at": finished,
                    }
                ).model_dump()
            )
        artifact = self._agent_response_artifact(state, finished)
        audit = self._agent_run_audit_event(
            state.agent_run_id,
            state.agent_version,
            state.artifact_id,
            finished,
            state.omitted_context_source_ids,
            state.requested_by_principal_id,
            state.run_id,
            state.skipped_capability_ids,
            state.truncated_context_source_ids,
            state.truncated_workflow_reference_ids,
            state.finalize_run,
            state.workflow_step_count,
            state.workflow_step_index,
        )
        with self._unit_of_work():
            self.agent_runs.put(state.agent_run)
            if state.finalize_run:
                self.runs.put(state.run)
            self.artifacts.create(artifact)
            self.audit_events.create(audit)
        return RuntimeResult(
            state.run,
            state.agent_run,
            artifact,
            tuple(state.invocations),
            audit,
            workflow_agent_runs=(state.agent_run,),
            workflow_artifacts=(artifact,),
        )
