"""Pure edit rules for canvas start controls, including timezone changes.

Timezone rule: naming a different zone never moves the pending UTC instant. The
new zone applies to the occurrence after the pending one (the tick computes it
when the pending one fires). Only an explicit edit of the calendar (frequency,
days or time of day), or turning the schedule on, recomputes the pending
occurrence from now in the effective zone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import JsonValue

from contracts.v1.timezones import validate_timezone_name
from contracts.v1.workflow_studio import Calendar, CanvasSettings
from modules.workflows.schedule_edit import require_current_revision
from modules.workflows.schedule_time import (
    calendar_identity,
    effective_timezone,
    next_occurrence,
)


def _pending_run(
    current: CanvasSettings, merged: CanvasSettings, now: datetime
) -> datetime | None:
    """Pending instant: kept unless the user explicitly changed what it means."""
    if not merged.sched:
        return None
    rule = Calendar.model_validate(merged.calendar)
    before = Calendar.model_validate(current.calendar)
    keeps = (
        current.sched
        and current.next_run_at is not None
        and calendar_identity(rule) == calendar_identity(before)
    )
    if keeps:
        return current.next_run_at
    return next_occurrence(rule, now, merged.timezone)


def apply_settings_changes(
    current: CanvasSettings,
    changes: dict[str, JsonValue] | dict[str, Any],
    *,
    now: datetime,
    stored: bool,
) -> CanvasSettings:
    """Return validated settings with the pending occurrence resolved."""
    edits = dict(changes)
    expected = edits.pop("expected_updated_at", None)
    requested = edits.pop("timezone", None)
    zone = validate_timezone_name(requested) if requested is not None else None
    explicit = zone is not None and zone != current.timezone
    if explicit and zone != effective_timezone(current.timezone) and stored:
        require_current_revision(current.updated_at, expected)
    merged = CanvasSettings.model_validate(
        current.model_copy(
            update={
                **edits,
                "timezone": zone if explicit else current.timezone,
                "updated_at": now,
            }
        ).model_dump()
    )
    merged.next_run_at = _pending_run(current, merged, now)
    return merged
