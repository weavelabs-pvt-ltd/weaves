"""Cross-organization reads and bounded deletes of ``slack_feedback`` (design 3.13).

The collection itself is an ordinary organization-scoped repository. These three
operations are the only ones that must reach every organization, so they run
under the named system purpose ``feedback`` (which the row policy limits to this
one collection): the 400-day retention delete, the analytics counts and the
debugging lookup by request id. None of them returns a user identifier or text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

PERIODS = ("day", "month")


@dataclass(frozen=True)
class FeedbackCount:
    """Thumbs counted for one period, organization, agent, model and connector set."""

    period: str
    org_id: str
    agent_id: str
    model_id: str
    connector_set: str
    up: int
    down: int


class FeedbackAdmin(Protocol):
    """The system-purpose operations over every organization's feedback."""

    def expire(self, before: datetime, limit: int) -> int:
        """Delete up to ``limit`` rows rated before ``before``; returns the count."""
        ...

    def counts(
        self, start: date, end: date, *, by: str, org_id: str | None = None
    ) -> list[FeedbackCount]:
        """Thumbs per period (UTC ``day`` or ``month``) and dimension, inclusive."""
        ...

    def by_request(self, request_id: str) -> list[dict[str, Any]]:
        """The rows of one request id: ids, value and time, never an asker."""
        ...
