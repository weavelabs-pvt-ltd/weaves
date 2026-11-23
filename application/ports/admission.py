"""Counting contract behind atomic admission.

Admission counts the source of truth (the stored rows) inside the transaction
that inserts the new row, so there is no separate counter that a crash could
leak. Implementations count only what ``resource`` names for one organization.
"""

from __future__ import annotations

from typing import Protocol

# Resources whose usage is a count of stored rows.
QUEUED_WORK = "queued_work"
TASKS = "tasks"
SEATS = "seats"
SCHEDULES = "schedules"
AGENTS = "agents"
WORKFLOWS = "workflows"
TOOL_CALLS_PER_DAY = "tool_calls_per_day"

COUNTED_RESOURCES: tuple[str, ...] = (
    QUEUED_WORK,
    TASKS,
    SEATS,
    SCHEDULES,
    AGENTS,
    WORKFLOWS,
    TOOL_CALLS_PER_DAY,
)


class AdmissionCounts(Protocol):
    """Count one organization's rows for a limited resource."""

    def count(self, org_id: str, resource: str) -> int:
        """Return how many rows of ``resource`` the organization holds now."""
        ...

    def global_queued(self) -> int:
        """Return queued work across every organization (approximate, no lock)."""
        ...
