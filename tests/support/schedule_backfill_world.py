"""Seed legacy and zoned schedule/calendar records for the backfill tests."""

from __future__ import annotations

from typing import Any

from contracts.v1 import WorkflowSchedule, WorkflowScheduleStatus
from contracts.v1.workflow_studio import CanvasSettings
from shared.clock import utc_now
from shared.request_context import system_scope


def seed(runtime: Any, org_id: str = "local-org") -> dict[str, Any]:
    """Create one legacy and one zoned record of each kind; return them."""
    now = utc_now()
    base: dict[str, Any] = {"org_id": org_id, "workspace_id": "local-workspace"}
    schedules = [
        WorkflowSchedule(
            schedule_id=f"sched-{name}",
            workflow_id="local-assistant-workflow",
            created_by_principal_id="local-developer",
            name=name,
            task_instructions="Run.",
            interval_seconds=3600,
            status=WorkflowScheduleStatus.ACTIVE,
            next_run_at=now,
            timezone=zone,
            created_at=now,
            updated_at=now,
            **base,
        )
        for name, zone in (("legacy", None), ("zoned", "America/New_York"))
    ]
    settings = [
        CanvasSettings(
            workflow_id=f"wf-{name}",
            principal_id="local-developer",
            calendar={"freq": "daily", "days": [0], "time": "09:00"},
            sched=True,
            next_run_at=now,
            timezone=zone,
            created_at=now,
            updated_at=now,
            **base,
        )
        for name, zone in (("legacy", None), ("zoned", "Europe/London"))
    ]
    with system_scope("test"):
        for record in schedules:
            runtime.workflow_schedules.put(record)
        for item in settings:
            runtime.canvas_settings.put(item)
    return {"schedules": schedules, "settings": settings}
