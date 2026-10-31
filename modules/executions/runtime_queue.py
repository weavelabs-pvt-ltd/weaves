"""Execution job lifecycle capability use cases."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    AuditEvent,
    AuditTargetScope,
    ConversationThreadStatus,
    ExecutionJob,
    ExecutionJobStatus,
    Permission,
    WorkflowStatus,
    WorkflowVersion,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import IdempotencyConflict, IdempotencyKeyAlreadyExists
from shared.request_context import effective_org_id, effective_workspace_id


class ExecutionQueueMixin(RuntimeCapabilityHost):
    """Own execution queue operations."""

    def enqueue_agent_run(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        trigger_id: str | None = None,
        trigger_event_id: str | None = None,
        schedule_id: str | None = None,
        schedule_occurrence_at: datetime | None = None,
        _idempotency_lock_held: bool = False,
    ) -> tuple[ExecutionJob, bool]:
        """Persist a run request for a separate execution worker."""
        self._ensure_bootstrapped()
        self.authorize(requested_by_principal_id, Permission.AGENTS_RUN)
        task = task.strip()
        if not task or len(task) > 12_000:
            raise ValueError("task must contain 1 to 12000 characters")
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > 128:
                raise ValueError("Idempotency-Key must contain 1 to 128 characters")
            if not _idempotency_lock_held:
                lock_key = (requested_by_principal_id, idempotency_key)
                with self._idempotency_guard:
                    lock = self._execution_job_idempotency_locks.setdefault(
                        lock_key, RLock()
                    )
                with lock:
                    return self.enqueue_agent_run(
                        task,
                        requested_by_principal_id,
                        agent_id,
                        thread_id,
                        idempotency_key,
                        workflow_id=workflow_id,
                        trigger_id=trigger_id,
                        trigger_event_id=trigger_event_id,
                        schedule_id=schedule_id,
                        schedule_occurrence_at=schedule_occurrence_at,
                        _idempotency_lock_held=True,
                    )
        return self._enqueue_agent_run_once(
            task,
            requested_by_principal_id,
            agent_id,
            thread_id,
            idempotency_key,
            workflow_id,
            trigger_id,
            trigger_event_id,
            schedule_id,
            schedule_occurrence_at,
        )

    def _enqueue_agent_run_once(
        self,
        task: str,
        requested_by_principal_id: str,
        agent_id: str | None,
        thread_id: str | None,
        idempotency_key: str | None,
        workflow_id: str | None,
        trigger_id: str | None,
        trigger_event_id: str | None,
        schedule_id: str | None,
        schedule_occurrence_at: datetime | None,
    ) -> tuple[ExecutionJob, bool]:
        """Resolve, validate, and persist one locked execution request."""
        _, pinned_workflow_version_id, selected_agent_id, version = (
            self._load_enqueued_agent_target(
                requested_by_principal_id, agent_id, thread_id, workflow_id
            )
        )
        if idempotency_key is not None:
            existing = self.execution_jobs.find_by_idempotency_key(
                idempotency_key,
                requested_by_principal_id,
                effective_org_id(),
                effective_workspace_id(),
            )
            if existing is not None:
                self._validate_job_replay(
                    existing,
                    task,
                    selected_agent_id,
                    thread_id,
                    workflow_id,
                    trigger_id,
                    trigger_event_id,
                    schedule_id,
                    schedule_occurrence_at,
                )
                return existing, True
        job = self._build_queued_execution_job(
            requested_by_principal_id,
            task,
            selected_agent_id,
            workflow_id,
            pinned_workflow_version_id,
            thread_id,
            trigger_id,
            trigger_event_id,
            schedule_id,
            schedule_occurrence_at,
            idempotency_key,
        )
        try:
            self._persist_queued_execution_job(
                job, version.agent_version_id, requested_by_principal_id
            )
        except IdempotencyKeyAlreadyExists:
            if idempotency_key is None:
                raise
            existing = self.execution_jobs.find_by_idempotency_key(
                idempotency_key,
                requested_by_principal_id,
                effective_org_id(),
                effective_workspace_id(),
            )
            if existing is None:
                raise
            self._validate_job_replay(
                existing,
                task,
                selected_agent_id,
                thread_id,
                workflow_id,
                trigger_id,
                trigger_event_id,
                schedule_id,
                schedule_occurrence_at,
            )
            return existing, True
        return job, False

    @staticmethod
    def _build_queued_execution_job(
        actor: str,
        task: str,
        agent_id: str,
        workflow_id: str | None,
        workflow_version_id: str | None,
        thread_id: str | None,
        trigger_id: str | None,
        trigger_event_id: str | None,
        schedule_id: str | None,
        schedule_occurrence_at: datetime | None,
        idempotency_key: str | None,
    ) -> ExecutionJob:
        """Create the queued execution record from its resolved request."""
        timestamp = _now()
        return ExecutionJob(
            job_id=_id(),
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            requested_by_principal_id=actor,
            task=task,
            agent_id=agent_id if workflow_id is None else None,
            workflow_id=workflow_id,
            workflow_version_id=workflow_version_id,
            thread_id=thread_id,
            trigger_id=trigger_id,
            trigger_event_id=trigger_event_id,
            schedule_id=schedule_id,
            schedule_occurrence_at=schedule_occurrence_at,
            idempotency_key=idempotency_key,
            status=ExecutionJobStatus.QUEUED,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def _load_enqueued_agent_target(
        self,
        actor: str,
        agent_id: str | None,
        thread_id: str | None,
        workflow_id: str | None,
    ) -> tuple[AgentDefinition, str | None, str, AgentVersion]:
        """Resolve the selected agent and immutable version for a queued run."""
        definition, workflow_version_id, selected_agent_id, _ = (
            self._resolve_enqueued_workflow(actor, agent_id, thread_id, workflow_id)
        )
        if thread_id:
            thread = self.threads.get_scoped(
                thread_id, effective_org_id(), effective_workspace_id()
            )
            if thread.status is not ConversationThreadStatus.OPEN:
                raise ValueError("conversation thread is closed")
            if thread.agent_id != selected_agent_id:
                raise ValueError("thread is bound to a different agent")
        if definition.status is not AgentStatus.ACTIVE:
            raise ValueError("agent must be active to run")
        version = self.agent_versions.get_scoped(
            definition.current_version_id or "",
            effective_org_id(),
            effective_workspace_id(),
        )
        return definition, workflow_version_id, selected_agent_id, version

    def _persist_queued_execution_job(
        self, job: ExecutionJob, agent_version_id: str, actor: str
    ) -> None:
        """Persist an execution job and its audit event atomically."""
        with self._unit_of_work():
            self.execution_jobs.create(job)
            self.audit_events.create(
                self._queued_execution_audit(job, agent_version_id, actor)
            )

    @staticmethod
    def _queued_execution_audit(
        job: ExecutionJob, agent_version_id: str, actor: str
    ) -> AuditEvent:
        """Build the audit event describing a queued execution job."""
        metadata: dict[str, Any] = {
            "agent_version_id": agent_version_id,
            **(
                {
                    "workflow_id": job.workflow_id,
                    "workflow_version_id": job.workflow_version_id,
                }
                if job.workflow_id is not None
                else {"agent_id": job.agent_id}
            ),
        }
        if job.trigger_id is not None:
            metadata["trigger_id"] = job.trigger_id
        if job.schedule_id is not None:
            metadata["schedule_id"] = job.schedule_id
        return AuditEvent(
            audit_event_id=_id(),
            org_id=job.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=job.workspace_id,
            actor_principal_id=actor,
            action=(
                "workflow.execution_job.queued"
                if job.workflow_id is not None
                else "agent.execution_job.queued"
            ),
            target_type="execution_job",
            target_id=job.job_id,
            summary="Agent run request queued",
            request_id=job.job_id,
            metadata=metadata,
            created_at=job.created_at,
        )

    def _resolve_enqueued_workflow(
        self,
        requested_by_principal_id: Any,
        agent_id: Any,
        thread_id: Any,
        workflow_id: Any,
    ) -> tuple[Any, ...]:
        """Resolve the workflow pinned by an enqueued agent run."""
        thread = None
        if workflow_id is not None:
            return self._resolve_enqueued_workflow_definition(
                requested_by_principal_id, agent_id, workflow_id
            )
        else:
            if thread_id:
                thread = self.threads.get_scoped(
                    thread_id, effective_org_id(), effective_workspace_id()
                )
                if thread.status is not ConversationThreadStatus.OPEN:
                    raise ValueError("conversation thread is closed")
                if agent_id and agent_id != thread.agent_id:
                    raise ValueError("thread is bound to a different agent")
                selected_agent_id = thread.agent_id
            else:
                selected_agent_id = agent_id or self._default_agent_id()
            definition = self.agents.get_scoped(
                selected_agent_id, effective_org_id(), effective_workspace_id()
            )
        return definition, None, selected_agent_id, thread

    def _resolve_enqueued_workflow_definition(
        self,
        requested_by_principal_id: str,
        agent_id: str | None,
        workflow_id: str,
    ) -> tuple[AgentDefinition, str, str, None]:
        """Resolve an active workflow and validate its pinned agent versions."""
        if agent_id is not None:
            raise ValueError("choose either agent_id or workflow_id")
        self.authorize(requested_by_principal_id, Permission.WORKFLOWS_RUN)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        if workflow.status is not WorkflowStatus.ACTIVE:
            raise ValueError("workflow must be active to run")
        if workflow.current_version_id is None:
            raise ValueError("workflow has no published version")
        version = self.workflow_versions.get_scoped(
            workflow.current_version_id, workflow.org_id, workflow.workspace_id
        )
        self._validate_workflow_handler(workflow_id, version)
        resolved_versions = tuple(
            self.agent_versions.get_scoped(item, workflow.org_id, workflow.workspace_id)
            for item in version.agent_version_ids
        )
        resolved_agents = tuple(
            self.agents.get_scoped(
                item.agent_id, workflow.org_id, workflow.workspace_id
            )
            for item in resolved_versions
        )
        self._validate_enqueued_workflow_bindings(
            workflow_id, version, resolved_versions, resolved_agents
        )
        return (
            resolved_agents[0],
            version.workflow_version_id,
            resolved_versions[0].agent_id,
            None,
        )

    @staticmethod
    def _validate_enqueued_workflow_bindings(
        workflow_id: str,
        workflow_version: WorkflowVersion,
        agent_versions: tuple[AgentVersion, ...],
        agents: tuple[AgentDefinition, ...],
    ) -> None:
        """Ensure all pinned workflow versions still match active agents."""
        if any(workflow_id not in item.allowed_workflow_ids for item in agent_versions):
            raise ValueError(
                "workflow is not allowed by one of its published agent versions"
            )
        capabilities = {
            capability_id
            for version in agent_versions
            for capability_id in version.allowed_capability_ids
        }
        if set(workflow_version.required_capability_ids).difference(capabilities):
            raise ValueError("workflow requires capabilities not allowed by its agents")
        if any(
            agent.status is not AgentStatus.ACTIVE
            or agent.current_version_id != version.agent_version_id
            for version, agent in zip(agent_versions, agents, strict=True)
        ):
            raise ValueError("workflow references an inactive or outdated agent")

    def _validate_job_replay(
        self,
        job: ExecutionJob,
        task: str,
        agent_id: str,
        thread_id: str | None,
        workflow_id: str | None,
        trigger_id: str | None,
        trigger_event_id: str | None,
        schedule_id: str | None,
        schedule_occurrence_at: datetime | None,
    ) -> None:
        target_matches = (
            job.workflow_id == workflow_id
            if workflow_id is not None
            else job.agent_id == agent_id
        )
        if (
            job.task != task
            or not target_matches
            or job.thread_id != thread_id
            or job.trigger_id != trigger_id
            or job.trigger_event_id != trigger_event_id
            or job.schedule_id != schedule_id
            or job.schedule_occurrence_at != schedule_occurrence_at
        ):
            raise IdempotencyConflict(
                "Idempotency-Key was already used for a different run request"
            )
