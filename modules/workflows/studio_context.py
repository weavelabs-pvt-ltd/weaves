"""Typed dependencies, authorization, queries and calendar helpers for canvas runs."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from threading import RLock

from pydantic import JsonValue

from application.ports.repository import Repository
from contracts.v1 import (
    Artifact,
    AuditEvent,
    AuditTargetScope,
    Permission,
)
from contracts.v1.workflow_studio import (
    Calendar,
    CanvasPlan,
    CanvasRun,
    CanvasSettings,
)
from modules.tasks.service import TaskService
from modules.workflows.definition_service import WorkflowDefinitionService
from modules.workflows.schedule_time import (
    next_occurrences,
    organization_default_timezone,
    resolve_zone,
)
from shared.clock import new_id, utc_now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowStudioContext(ABC):
    """Typed shared dependencies and authorization for canvas operations."""

    def __init__(
        self,
        *,
        definitions: WorkflowDefinitionService,
        tasks: TaskService,
        runs: Repository[CanvasRun],
        settings: Repository[CanvasSettings],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        lock: Callable[[str], None] | None = None,
        artifacts: Repository[Artifact] | None = None,
        organization_settings: object | None = None,
    ) -> None:
        """Bind persistence, authorization, task execution and transaction ports."""
        self.definitions, self.tasks = definitions, tasks
        self.runs, self.settings = runs, settings
        self.unit_of_work, self.lock = unit_of_work, lock
        self.local_lock = RLock()
        self.artifacts = artifacts
        self.organization_settings = organization_settings

    def authorize(self, permission: Permission) -> None:
        """Enforce workspace permissions at every public operation."""
        self.definitions.authorize(
            effective_principal_id("local-developer"), permission
        )

    @contextmanager
    def guard(self, key: str) -> Iterator[None]:
        """Serialize publication, dispatch, and decisions across processes."""
        with self.local_lock, self.unit_of_work():
            if self.lock:
                self.lock("workflow-studio:" + key)
            yield

    @staticmethod
    def next_dates(
        calendar: Calendar | dict[str, JsonValue],
        now: datetime | None = None,
        timezone: str | None = None,
    ) -> list[datetime]:
        """Next three occurrences in the zone (None = legacy Asia/Kolkata).

        Returned in the zone's local offset; persist them as UTC instants.
        """
        cal = Calendar.model_validate(calendar)
        zone = resolve_zone(timezone)
        found = next_occurrences(cal, now or utc_now(), timezone, 3)
        return [instant.astimezone(zone) for instant in found]

    def default_timezone(self) -> str:
        """The organization's zone for newly created calendars."""
        return organization_default_timezone(
            self.organization_settings, effective_org_id()
        )

    def get_settings(self, workflow_id: str) -> CanvasSettings:
        """Read start controls only inside the active workspace."""
        self.authorize(Permission.WORKFLOWS_VIEW)
        workflow = self.definitions.get_workflow(workflow_id)
        try:
            return self.settings.get_scoped(
                workflow_id, workflow.org_id, workflow.workspace_id
            )
        except KeyError:
            version = self.definitions.get_current_workflow_version(workflow)
            raw_plan = dict(version.configuration).get("canvas")
            plan = CanvasPlan.model_validate(raw_plan) if raw_plan else None
            now = utc_now()
            return CanvasSettings(
                workflow_id=workflow_id,
                org_id=workflow.org_id,
                workspace_id=workflow.workspace_id,
                principal_id=effective_principal_id("local-developer"),
                timezone=self.default_timezone(),
                manual=plan.manual if plan else True,
                hook=plan.hook if plan else False,
                sched=plan.sched if plan else False,
                calendar=plan.calendar.model_dump()
                if plan
                else Calendar().model_dump(),
                created_at=now,
                updated_at=now,
            )

    def audit(self, action: str, target: str, summary: str) -> None:
        """Record human-visible workflow decisions without storing input content."""
        self.definitions.audit_events.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                target_scope=AuditTargetScope.WORKSPACE,
                actor_principal_id=effective_principal_id("local-developer"),
                action=action,
                target_type="workflow_run",
                target_id=target,
                summary=summary,
                request_id=target,
                metadata={},
                created_at=utc_now(),
            )
        )

    def list_runs(self, workflow_id: str) -> list[CanvasRun]:
        """Return the workflow's durable runs newest first."""
        self.authorize(Permission.WORKFLOWS_VIEW)
        self.definitions.get_workflow(workflow_id)
        return sorted(
            (
                r
                for r in self.runs.list_scoped(
                    effective_org_id(), effective_workspace_id()
                )
                if r.workflow_id == workflow_id
            ),
            key=lambda r: r.created_at,
            reverse=True,
        )[:100]

    def get_run(self, workflow_id: str, run_id: str) -> CanvasRun:
        """Prevent cross-workflow and cross-tenant run inspection."""
        self.authorize(Permission.WORKFLOWS_VIEW)
        run = self.runs.get_scoped(run_id, effective_org_id(), effective_workspace_id())
        if run.workflow_id != workflow_id:
            raise KeyError(run_id)
        return run

    @abstractmethod
    def start(
        self,
        workflow_id: str,
        instructions: str,
        context: list[str] | None = None,
        key: str | None = None,
        source: str = "manual",
        retry: str | None = None,
    ) -> CanvasRun:
        """Queue an authorized run through the execution service."""
        raise NotImplementedError

    @abstractmethod
    def advance(self, run: CanvasRun) -> None:
        """Advance one persisted workflow stage."""
        raise NotImplementedError
