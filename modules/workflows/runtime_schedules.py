"""Recurring workflow schedule lifecycle and dispatch operations."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta
from threading import RLock

from pydantic import JsonValue

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    ErrorSummary,
    ExecutionJob,
    Permission,
    WorkflowDefinition,
    WorkflowSchedule,
    WorkflowScheduleStatus,
    WorkflowStatus,
    WorkflowVersion,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
    reset_request_principal,
    set_request_identity,
)


class WorkflowScheduleRuntimeHost(ABC):
    """Dependencies required for workflow schedule dispatch."""

    _bootstrapped: Callable[[], bool]
    _advisory_transaction_lock: Callable[[str], None] | None
    _workflow_schedule_locks: dict[str, RLock]
    _workflow_schedule_locks_guard: RLock
    audit_events: Repository[AuditEvent]
    workflow_schedules: Repository[WorkflowSchedule]
    workflow_versions: Repository[WorkflowVersion]
    workflows: Repository[WorkflowDefinition]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize workflow defaults before schedule operations."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce the requested workflow permission."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for schedule and audit writes."""
        raise NotImplementedError

    @abstractmethod
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
        """Enqueue a scheduled workflow execution."""
        raise NotImplementedError


class WorkflowScheduleRuntimeMixin(WorkflowScheduleRuntimeHost):
    """Create, update, and dispatch interval-based workflow schedules."""

    @contextmanager
    def _workflow_schedule_guard(self, schedule_id: str) -> Iterator[None]:
        """Serialize a due occurrence across worker processes."""
        if self._advisory_transaction_lock is not None:
            with self._unit_of_work():
                self._advisory_transaction_lock(f"workflow-schedule:{schedule_id}")
                yield
            return
        with self._workflow_schedule_locks_guard:
            lock = self._workflow_schedule_locks.setdefault(schedule_id, RLock())
        with lock:
            yield

    def create_workflow_schedule(
        self,
        workflow_id: str,
        *,
        name: str,
        task_instructions: str,
        interval_seconds: int,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowSchedule:
        """Create a recurring interval schedule for an active workflow."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_MANAGE)
        self.authorize(actor, Permission.WORKFLOWS_RUN)
        self.authorize(actor, Permission.AGENTS_RUN)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        if workflow.status is not WorkflowStatus.ACTIVE:
            raise ValueError("workflow must be active to create a schedule")
        if workflow.current_version_id is None:
            raise ValueError("workflow has no published version")
        self.workflow_versions.get_scoped(
            workflow.current_version_id, workflow.org_id, workflow.workspace_id
        )
        clean_name = name.strip()
        clean_instructions = task_instructions.strip()
        if not clean_name or len(clean_name) > 160:
            raise ValueError("schedule name must contain 1 to 160 characters")
        if not clean_instructions or len(clean_instructions) > 2000:
            raise ValueError("schedule instructions must contain 1 to 2000 characters")
        if not 60 <= interval_seconds <= 31_536_000:
            raise ValueError("interval_seconds must be between 60 and 31536000")
        now = _now()
        schedule = WorkflowSchedule(
            schedule_id=_id(),
            workflow_id=workflow_id,
            created_by_principal_id=actor,
            org_id=workflow.org_id,
            workspace_id=workflow.workspace_id,
            name=clean_name,
            task_instructions=clean_instructions,
            interval_seconds=interval_seconds,
            status=WorkflowScheduleStatus.ACTIVE,
            next_run_at=now + timedelta(seconds=interval_seconds),
            created_at=now,
            updated_at=now,
        )
        with self._unit_of_work():
            self.workflow_schedules.create(schedule)
            self._record_workflow_schedule_audit(
                schedule,
                actor,
                "workflow.schedule.created",
                "Workflow schedule created",
                metadata={"interval_seconds": interval_seconds},
            )
        return schedule

    def list_workflow_schedules(self) -> tuple[WorkflowSchedule, ...]:
        """List schedules in the selected workspace for workflow administrators."""
        if not self._bootstrapped():
            self.bootstrap()
        self.authorize(
            effective_principal_id("local-developer"), Permission.WORKFLOWS_MANAGE
        )
        schedules = self.workflow_schedules.list_scoped(
            effective_org_id(), effective_workspace_id()
        )
        return tuple(sorted(schedules, key=lambda item: item.created_at, reverse=True))

    def update_workflow_schedule(
        self,
        schedule_id: str,
        status: WorkflowScheduleStatus,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowSchedule:
        """Enable or disable a schedule; re-enabling starts a fresh interval."""
        if status is WorkflowScheduleStatus.ERROR:
            raise ValueError("schedule status can only be set to active or disabled")
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_MANAGE)
        with self._workflow_schedule_guard(schedule_id):
            current = self.workflow_schedules.get_scoped(
                schedule_id, effective_org_id(), effective_workspace_id()
            )
            if current.status is status:
                return current
            timestamp = _now()
            updated = WorkflowSchedule.model_validate(
                current.model_copy(
                    update={
                        "status": status,
                        "next_run_at": (
                            timestamp + timedelta(seconds=current.interval_seconds)
                            if status is WorkflowScheduleStatus.ACTIVE
                            else current.next_run_at
                        ),
                        "last_error": None,
                        "updated_at": timestamp,
                    }
                ).model_dump()
            )
            with self._unit_of_work():
                self.workflow_schedules.put(updated)
                self._record_workflow_schedule_audit(
                    updated,
                    actor,
                    "workflow.schedule.updated",
                    f"Workflow schedule {status.value}",
                    metadata={},
                )
            return updated

    def _record_workflow_schedule_audit(
        self,
        schedule: WorkflowSchedule,
        actor: str,
        action: str,
        summary: str,
        *,
        metadata: dict[str, JsonValue],
    ) -> None:
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=schedule.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=schedule.workspace_id,
                actor_principal_id=actor,
                action=action,
                target_type="workflow_schedule",
                target_id=schedule.schedule_id,
                summary=summary,
                request_id=schedule.schedule_id,
                metadata={"workflow_id": schedule.workflow_id, **metadata},
                created_at=_now(),
            )
        )

    def dispatch_due_workflow_schedules(
        self, *, now: datetime | None = None, limit: int = 100
    ) -> tuple[ExecutionJob, ...]:
        """Atomically enqueue due schedules, coalescing missed intervals."""
        if not self._bootstrapped():
            self.bootstrap()
        if not 1 <= limit <= 100:
            raise ValueError("schedule dispatch limit must be between 1 and 100")
        timestamp = now or _now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("schedule dispatch time must include a timezone")
        due_schedules = self.workflow_schedules.list_due(timestamp, limit=limit)
        queued: list[ExecutionJob] = []
        for schedule in due_schedules:
            job = self._dispatch_workflow_schedule(schedule.schedule_id, timestamp)
            if job is not None:
                queued.append(job)
        return tuple(queued)

    def _dispatch_workflow_schedule(
        self, schedule_id: str, timestamp: datetime
    ) -> ExecutionJob | None:
        with self._workflow_schedule_guard(schedule_id):
            try:
                schedule = self.workflow_schedules.get(schedule_id)
            except KeyError:
                return None
            if (
                schedule.status is not WorkflowScheduleStatus.ACTIVE
                or schedule.next_run_at > timestamp
            ):
                return None
            occurrence = schedule.next_run_at
            task = (
                f"{schedule.task_instructions}\n\n"
                f"Scheduled occurrence: {occurrence.isoformat()}."
            )
            idempotency_key = (
                "schedule_"
                + hashlib.sha256(
                    f"{schedule.schedule_id}\0{occurrence.isoformat()}".encode()
                ).hexdigest()
            )
            job = self._enqueue_schedule_occurrence(
                schedule, timestamp, task, idempotency_key
            )
            if job is None:
                return None

            updated = WorkflowSchedule.model_validate(
                schedule.model_copy(
                    update={
                        "last_run_at": occurrence,
                        "next_run_at": timestamp
                        + timedelta(seconds=schedule.interval_seconds),
                        "updated_at": timestamp,
                    }
                ).model_dump()
            )
            with self._unit_of_work():
                self.workflow_schedules.put(updated)
                self._record_workflow_schedule_audit(
                    updated,
                    schedule.created_by_principal_id,
                    "workflow.schedule.dispatched",
                    "Scheduled workflow execution queued",
                    metadata={
                        "execution_job_id": job.job_id,
                        "occurrence_at": occurrence.isoformat(),
                    },
                )
            return job

    def _enqueue_schedule_occurrence(
        self,
        schedule: WorkflowSchedule,
        timestamp: datetime,
        task: str,
        idempotency_key: str,
    ) -> ExecutionJob | None:
        identity = set_request_identity(
            schedule.created_by_principal_id,
            schedule.org_id,
            schedule.workspace_id,
        )
        try:
            return self._try_enqueue_schedule_occurrence(
                schedule, timestamp, task, idempotency_key
            )
        finally:
            reset_request_principal(identity)

    def _try_enqueue_schedule_occurrence(
        self,
        schedule: WorkflowSchedule,
        timestamp: datetime,
        task: str,
        idempotency_key: str,
    ) -> ExecutionJob | None:
        try:
            job, _ = self.enqueue_agent_run(
                task=task,
                requested_by_principal_id=schedule.created_by_principal_id,
                workflow_id=schedule.workflow_id,
                idempotency_key=idempotency_key,
                schedule_id=schedule.schedule_id,
                schedule_occurrence_at=schedule.next_run_at,
            )
            return job
        except PermissionError:
            self._fail_workflow_schedule(
                schedule,
                actor=schedule.created_by_principal_id,
                timestamp=timestamp,
                code="schedule.permission_revoked",
                summary=(
                    "The schedule creator no longer has permission to run this "
                    "workflow."
                ),
            )
        except KeyError:
            self._fail_workflow_schedule(
                schedule,
                actor=schedule.created_by_principal_id,
                timestamp=timestamp,
                code="schedule.workflow_unavailable",
                summary=(
                    "The scheduled workflow configuration is no longer available."
                ),
            )
        except ValueError:
            self._fail_workflow_schedule(
                schedule,
                actor=schedule.created_by_principal_id,
                timestamp=timestamp,
                code="schedule.workflow_unavailable",
                summary="The scheduled workflow is no longer eligible to run.",
            )
        return None

    def _fail_workflow_schedule(
        self,
        schedule: WorkflowSchedule,
        *,
        actor: str,
        timestamp: datetime,
        code: str,
        summary: str,
    ) -> None:
        updated = WorkflowSchedule.model_validate(
            schedule.model_copy(
                update={
                    "status": WorkflowScheduleStatus.ERROR,
                    "last_error": ErrorSummary(
                        code=code, summary=summary, retryable=False
                    ),
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.workflow_schedules.put(updated)
            self._record_workflow_schedule_audit(
                updated,
                actor,
                "workflow.schedule.failed",
                summary,
                metadata={"error_code": code},
            )
        return None
