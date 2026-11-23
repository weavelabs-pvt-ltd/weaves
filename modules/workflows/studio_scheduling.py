"""Resume active canvas runs and dispatch scheduled workflow occurrences."""

from datetime import UTC
from typing import Any

from application.pagination import batches, drain, recovery_batch_size
from contracts.v1 import WorkflowStatus
from contracts.v1.workflow_studio import CanvasRun, CanvasSettings
from modules.limits.errors import LimitExceeded
from modules.workflows.studio_context import WorkflowStudioContext
from shared.clock import utc_now
from shared.request_context import (
    reset_request_principal,
    set_request_identity,
    system_scope,
)


def _run_position(run: CanvasRun) -> tuple[Any, ...]:
    return (run.run_id,)


class WorkflowStudioSchedulingMixin(WorkflowStudioContext):
    """Resume persisted runs and calendar occurrences."""

    def tick(self) -> None:
        """Resume queued work and dispatch due calendar occurrences after restart.

        Reads only due schedules and active runs, a bounded batch at a time, so
        the cost follows live work rather than the size of the history.
        """
        batch = recovery_batch_size()
        self._dispatch_due_settings(batch)
        self._advance_active_runs(batch)

    def _dispatch_due_settings(self, batch: int) -> None:
        """Start every calendar occurrence that is due, a batch at a time."""
        handled: set[str] = set()

        def fetch() -> tuple[CanvasSettings, ...]:
            with system_scope("studio"):
                due = self.settings.list_before(
                    "next_run_at", utc_now(), limit=batch, equals={"sched": "true"}
                )
            return tuple(item for item in due if item.workflow_id not in handled)

        def handle(settings: CanvasSettings) -> bool:
            handled.add(settings.workflow_id)
            self._dispatch_setting(settings)
            return True

        drain(fetch, handle, batch_size=batch)

    def _advance_active_runs(self, batch: int) -> None:
        """Advance queued and running canvas runs, walking each status by key."""
        for state in ("queued", "running"):

            def fetch(
                after: tuple[Any, ...] | None, state: str = state
            ) -> tuple[CanvasRun, ...]:
                return self._active_runs(state, batch, after)

            for run in batches(
                fetch, _run_position, batch_size=batch, max_batches=1_000
            ):
                self._advance_run(run)

    def _active_runs(
        self, state: str, batch: int, after: tuple[Any, ...] | None
    ) -> tuple[CanvasRun, ...]:
        with system_scope("studio"):
            return self.runs.list_by_status(
                state, limit=batch, after_id=str(after[0]) if after else None
            )

    def _dispatch_setting(self, settings: CanvasSettings) -> None:
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
                    return
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

    def _advance_run(self, run: CanvasRun) -> None:
        token = set_request_identity(
            run.requested_by_principal_id, run.org_id, run.workspace_id
        )
        try:
            self.advance(run)
        except LimitExceeded:
            # The organization is at a limit: leave the run as it is and try
            # again on the next tick (child task keys make the retry safe).
            return
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
