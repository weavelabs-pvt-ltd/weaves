"""Workflow webhook and recurring schedule use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from threading import RLock
from typing import Protocol

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    ExecutionJob,
    Permission,
    WorkflowDefinition,
    WorkflowSchedule,
    WorkflowTrigger,
    WorkflowVersion,
)
from modules.workflows.runtime_schedules import WorkflowScheduleRuntimeMixin
from modules.workflows.runtime_triggers import WorkflowTriggerRuntimeMixin


class AgentRunEnqueuer(Protocol):
    """Execution operation required by trigger and schedule dispatch."""

    def __call__(
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
        """Enqueue one contextualized workflow run."""
        ...


class WorkflowAutomationService(
    WorkflowTriggerRuntimeMixin, WorkflowScheduleRuntimeMixin
):
    """Manage webhook triggers and schedules using explicit application ports."""

    audit_events: Repository[AuditEvent]
    workflow_triggers: Repository[WorkflowTrigger]
    workflow_schedules: Repository[WorkflowSchedule]
    workflow_versions: Repository[WorkflowVersion]
    workflows: Repository[WorkflowDefinition]

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        workflow_triggers: Repository[WorkflowTrigger],
        workflow_schedules: Repository[WorkflowSchedule],
        workflow_versions: Repository[WorkflowVersion],
        workflows: Repository[WorkflowDefinition],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        advisory_transaction_lock: Callable[[str], None] | None,
        enqueue_agent_run: AgentRunEnqueuer,
    ) -> None:
        """Bind repositories, request policies, and execution dispatch ports."""
        self.audit_events = audit_events
        self.workflow_triggers = workflow_triggers
        self.workflow_schedules = workflow_schedules
        self.workflow_versions = workflow_versions
        self.workflows = workflows
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._open_unit_of_work = unit_of_work
        self._advisory_transaction_lock = advisory_transaction_lock
        self._enqueue_agent_run_impl = enqueue_agent_run
        self._workflow_trigger_locks: dict[str, RLock] = {}
        self._workflow_trigger_locks_guard = RLock()
        self._workflow_schedule_locks: dict[str, RLock] = {}
        self._workflow_schedule_locks_guard = RLock()

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workflow permissions through the identity capability."""
        self._authorize(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a persistence transaction for workflow automation changes."""
        return self._open_unit_of_work()

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
        """Enqueue trigger or schedule work using durable execution service."""
        studio = getattr(self, "studio", None)
        if studio is not None and workflow_id:
            definition = studio.definitions.get_workflow(workflow_id)
            version = studio.definitions.get_current_workflow_version(definition)
            if dict(version.configuration).get("canvas"):
                if trigger_id and not studio.get_settings(workflow_id).hook:
                    raise ValueError("This workflow webhook is paused")
                from shared.request_context import (
                    reset_request_principal,
                    set_request_identity,
                )

                token = set_request_identity(
                    requested_by_principal_id,
                    definition.org_id,
                    definition.workspace_id,
                )
                try:
                    existing = next(
                        (
                            r
                            for r in studio.list_runs(workflow_id)
                            if idempotency_key and r.idempotency_key == idempotency_key
                        ),
                        None,
                    )
                    result = studio.start(
                        workflow_id,
                        task,
                        key=idempotency_key,
                        source="webhook" if trigger_id else "schedule",
                    )
                    return result, existing is not None
                finally:
                    reset_request_principal(token)
        return self._enqueue_agent_run_impl(
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
            _idempotency_lock_held=_idempotency_lock_held,
        )
