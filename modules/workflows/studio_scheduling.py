"""Resume active canvas runs and dispatch scheduled workflow occurrences."""

from datetime import UTC

from contracts.v1 import (
    WorkflowStatus,
)
from modules.workflows.studio_context import WorkflowStudioContext
from shared.clock import utc_now
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)


class WorkflowStudioSchedulingMixin(WorkflowStudioContext):
    """Resume persisted runs and calendar occurrences."""

    def tick(self) -> None:
        """Resume queued work and dispatch due calendar occurrences after restart."""
        with system_scope("studio"):
            all_settings = tuple(self.settings.list())
            all_runs = tuple(self.runs.list())
        for settings in all_settings:
            if (
                not settings.sched
                or not settings.next_run_at
                or settings.next_run_at > utc_now()
            ):
                continue
            token = set_request_identity(
                settings.principal_id, settings.org_id, settings.workspace_id
            )
            try:
                with self.guard(settings.workflow_id):
                    current = self.settings.get_scoped(
                        settings.workflow_id, settings.org_id, settings.workspace_id
                    )
                    if (
                        not current.sched
                        or not current.next_run_at
                        or current.next_run_at > utc_now()
                    ):
                        continue
                    workflow = self.definitions.get_workflow(settings.workflow_id)
                    if workflow.status == WorkflowStatus.ACTIVE:
                        self.start(
                            settings.workflow_id,
                            workflow.description or workflow.name,
                            key=f"calendar:{settings.workflow_id}:{current.next_run_at.isoformat()}",
                            source="schedule",
                        )
                    current.last_run_at, current.next_run_at = (
                        utc_now(),
                        self.next_dates(current.calendar, timezone=current.timezone)[
                            0
                        ].astimezone(UTC),
                    )
                    current.updated_at = utc_now()
                    current.last_error = None
                    self.settings.put(current)
            except (PermissionError, KeyError, ValueError):
                with self.guard(settings.workflow_id):
                    current = self.settings.get_scoped(
                        settings.workflow_id, settings.org_id, settings.workspace_id
                    )
                    current.sched, current.next_run_at = False, None
                    current.last_error = (
                        "Schedule paused because its owner no longer has access "
                        "or its workflow configuration is unavailable."
                    )
                    current.updated_at = utc_now()
                    self.settings.put(current)
            finally:
                reset_request_principal(token)
        for run in all_runs:
            if run.status not in {"queued", "running"}:
                continue
            token = set_request_identity(
                run.requested_by_principal_id, run.org_id, run.workspace_id
            )
            try:
                self.advance(run)
            except Exception as exc:
                with self.guard(run.run_id):
                    latest = self.runs.get(run.run_id)
                    if latest.status in {"queued", "running"}:
                        latest.status, latest.error, latest.finished_at = (
                            "failed",
                            str(exc)[:1000],
                            utc_now(),
                        )
                        latest.updated_at = utc_now()
                        self.runs.put(latest)
            finally:
                reset_request_principal(token)
