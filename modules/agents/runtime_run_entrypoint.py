"""Idempotent agent-run entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, RLock

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentRun,
    AgentSkillSnapshot,
    Permission,
    RunStatus,
    WorkflowRun,
)
from modules.workflows.runtime_result import RuntimeResult
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class AgentRunEntrypointMixin(RuntimeCapabilityHost):
    """Run agents idempotently and rebuild results from durable records."""

    def run_agent(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        agent_version_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
        _runtime_skill_snapshots: tuple[AgentSkillSnapshot, ...] = (),
    ) -> RuntimeResult:
        """Execute once per principal/key and replay completed successful runs."""
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        if idempotency_key is None:
            return self._execute_agent_run(
                task,
                requested_by_principal_id,
                agent_id,
                thread_id,
                workflow_id=workflow_id,
                agent_version_id=agent_version_id,
                _cancellation_event=_cancellation_event,
                _cancellation_check=_cancellation_check,
                _runtime_skill_snapshots=_runtime_skill_snapshots,
            )
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("Idempotency-Key must contain 1 to 128 characters")
        if not self.is_bootstrapped():
            self.bootstrap()
        self.authorize(requested_by_principal_id, Permission.AGENTS_RUN)
        if workflow_id is not None:
            self.authorize(requested_by_principal_id, Permission.WORKFLOWS_RUN)
        return self._run_agent_with_key(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            key,
            workflow_id,
            agent_version_id,
            _cancellation_event,
            _cancellation_check,
            _runtime_skill_snapshots,
        )

    def _run_agent_with_key(
        self,
        task: str,
        requested_by_principal_id: str,
        agent_id: str | None,
        thread_id: str | None,
        key: str,
        workflow_id: str | None,
        agent_version_id: str | None,
        cancellation_event: Event | None,
        cancellation_check: Callable[[], bool] | None,
        runtime_skill_snapshots: tuple[AgentSkillSnapshot, ...],
    ) -> RuntimeResult:
        """Serialize idempotent requests and replay only matching runs."""
        lock_key = (requested_by_principal_id, key)
        with self._idempotency_guard:
            lock = self._idempotency_locks.setdefault(lock_key, RLock())
        with lock:
            existing = self.runs.find_by_idempotency_key(
                key,
                requested_by_principal_id,
                effective_org_id(),
                effective_workspace_id(),
            )
            if existing is not None:
                return self._replay_idempotent_run(
                    existing,
                    task=task.strip(),
                    agent_id=agent_id,
                    thread_id=thread_id,
                    workflow_id=workflow_id,
                    agent_version_id=agent_version_id,
                )
            try:
                return self._execute_agent_run(
                    task,
                    requested_by_principal_id,
                    agent_id,
                    thread_id,
                    idempotency_key=key,
                    workflow_id=workflow_id,
                    agent_version_id=agent_version_id,
                    _cancellation_event=cancellation_event,
                    _cancellation_check=cancellation_check,
                    _runtime_skill_snapshots=runtime_skill_snapshots,
                )
            except IdempotencyKeyAlreadyExists:
                # Another API process won the unique-index race. The losing
                # process has made no external call yet because the run row is
                # created before plugin/model execution.
                existing = self.runs.find_by_idempotency_key(
                    key,
                    requested_by_principal_id,
                    effective_org_id(),
                    effective_workspace_id(),
                )
                if existing is None:
                    raise
                return self._replay_idempotent_run(
                    existing,
                    task=task.strip(),
                    agent_id=agent_id,
                    thread_id=thread_id,
                    workflow_id=workflow_id,
                    agent_version_id=agent_version_id,
                )

    def _replay_idempotent_run(
        self,
        run: WorkflowRun,
        *,
        task: str,
        agent_id: str | None,
        thread_id: str | None,
        workflow_id: str | None,
        agent_version_id: str | None = None,
    ) -> RuntimeResult:
        agent_runs = self._validate_replayed_run_request(
            run, task, agent_id, thread_id, workflow_id, agent_version_id
        )
        artifacts = tuple(
            item
            for item in self.artifacts.list_scoped(run.org_id, run.workspace_id)
            if item.run_id == run.run_id
        )
        if len(artifacts) != len(agent_runs):
            raise RuntimeError("idempotent workflow has invalid step artifacts")
        artifacts_by_agent_run = {item.agent_run_id: item for item in artifacts}
        try:
            ordered_artifacts = tuple(
                artifacts_by_agent_run[item.agent_run_id] for item in agent_runs
            )
        except KeyError as exc:
            raise RuntimeError("idempotent workflow has an unlinked artifact") from exc
        invocations = tuple(
            item
            for item in self.tool_invocations.list_scoped(run.org_id, run.workspace_id)
            if item.run_id == run.run_id
        )
        audit_events = tuple(
            item
            for item in self.audit_events.list_scoped(run.org_id, run.workspace_id)
            if item.target_id == run.run_id and item.action == "agent.run.succeeded"
        )
        if not audit_events:
            raise RuntimeError("idempotent run has no completion audit event")
        return RuntimeResult(
            run=run,
            agent_run=agent_runs[-1],
            artifact=ordered_artifacts[-1],
            tool_invocations=invocations,
            audit_event=max(audit_events, key=lambda item: item.created_at),
            replayed=True,
            workflow_agent_runs=agent_runs,
            workflow_artifacts=ordered_artifacts,
        )

    def _validate_replayed_run_request(
        self,
        run: WorkflowRun,
        task: str,
        agent_id: str | None,
        thread_id: str | None,
        workflow_id: str | None,
        agent_version_id: str | None = None,
    ) -> tuple[AgentRun, ...]:
        """Verify the saved run matches the original idempotent request."""
        agent_runs = tuple(
            sorted(
                (
                    item
                    for item in self.agent_runs.list_scoped(
                        run.org_id, run.workspace_id
                    )
                    if item.run_id == run.run_id
                ),
                key=lambda item: int(item.input_summary.get("workflow_step_index", 0)),
            )
        )
        if not agent_runs:
            raise RuntimeError("idempotent run has no agent-run records")
        first_agent_run = agent_runs[0]
        agent_version = self.agent_versions.get_scoped(
            first_agent_run.agent_version_id, run.org_id, run.workspace_id
        )
        expected_workflow_id = (
            workflow_id
            if workflow_id is not None
            else agent_version.allowed_workflow_ids[0]
        )
        agent_matches = (
            True
            if workflow_id is not None
            else (
                first_agent_run.agent_id == (agent_id or self._default_agent_id())
                if thread_id is None
                else agent_id is None or first_agent_run.agent_id == agent_id
            )
        )
        payload_matches = (
            first_agent_run.input_summary.get("task") == task
            and run.trigger_ref == thread_id
            and run.workflow_id == expected_workflow_id
            and agent_matches
            and (
                agent_version_id is None
                or first_agent_run.agent_version_id == agent_version_id
            )
        )
        if not payload_matches:
            raise IdempotencyConflict(
                "Idempotency-Key was already used for a different run request"
            )
        if run.status is not RunStatus.SUCCEEDED:
            raise IdempotencyConflict(
                "The run for this Idempotency-Key did not succeed; "
                "use a new key to retry"
            )
        return agent_runs
