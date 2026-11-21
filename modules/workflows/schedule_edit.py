"""Pure edit rules for interval schedules, including timezone changes."""

from __future__ import annotations

from datetime import datetime, timedelta

from contracts.v1 import WorkflowSchedule, WorkflowScheduleStatus
from modules.workflows.schedule_time import effective_timezone
from shared.errors import StaleRevision

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 31_536_000


def require_current_revision(
    current_updated_at: datetime, expected: datetime | None
) -> None:
    """A timezone change must name the revision the editor observed."""
    if expected is None:
        raise ValueError("expected_updated_at is required when changing timezone")
    if expected != current_updated_at:
        raise StaleRevision("The record changed; reload it and retry")


def build_schedule_update(
    current: WorkflowSchedule,
    *,
    status: WorkflowScheduleStatus | None,
    name: str | None,
    task_instructions: str | None,
    interval_seconds: int | None,
    timezone: str | None,
    expected_updated_at: datetime | None,
    now: datetime,
) -> WorkflowSchedule:
    """Return the edited schedule, or ``current`` when nothing changes.

    Changes to name, instructions, interval or status restart the interval as
    before. A timezone-only change keeps the pending UTC instant untouched:
    interval math is elapsed-time based, so the zone is record metadata.
    """
    next_status = status or current.status
    next_name = name.strip() if name is not None else current.name
    next_text = (
        task_instructions.strip()
        if task_instructions is not None
        else current.task_instructions
    )
    next_interval = interval_seconds or current.interval_seconds
    if not next_name or not next_text:
        raise ValueError("schedule name and task instructions must not be blank")
    if not MIN_INTERVAL_SECONDS <= next_interval <= MAX_INTERVAL_SECONDS:
        raise ValueError("interval_seconds must be between 60 and 31536000")
    zone_changed = timezone is not None and timezone != effective_timezone(
        current.timezone
    )
    if zone_changed:
        require_current_revision(current.updated_at, expected_updated_at)
    restart = (
        current.status is not next_status
        or current.name != next_name
        or current.task_instructions != next_text
        or current.interval_seconds != next_interval
    )
    if not restart and not zone_changed:
        return current
    next_run = current.next_run_at
    if restart and next_status is WorkflowScheduleStatus.ACTIVE:
        next_run = now + timedelta(seconds=next_interval)
    return WorkflowSchedule.model_validate(
        current.model_copy(
            update={
                "status": next_status,
                "name": next_name,
                "task_instructions": next_text,
                "interval_seconds": next_interval,
                "timezone": timezone if zone_changed else current.timezone,
                "next_run_at": next_run,
                "last_error": None,
                "updated_at": now,
            }
        ).model_dump()
    )
