"""The ``review`` rows of ``slack_threads``: one per thread, the latest review's state.

Same collection and the same row shape as an Ask thread, a different kind in the
key, so a thread can hold one conversation and one review side by side. A new
review in the same thread replaces the previous review's request state (a review
is a task of its own, never a follow-up of the last one).
"""

from __future__ import annotations

from datetime import datetime

from application.ports.repository import Repository
from contracts.v1.slack_threads import SlackThread, thread_record_id

KIND = "review"


class ReviewBook:
    """Find, start and restart review rows."""

    def __init__(self, threads: Repository[SlackThread]) -> None:
        """Bind the (tenant-scoped) repository."""
        self._threads = threads

    @staticmethod
    def key(org_id: str, team_id: str, channel_id: str, thread_ts: str) -> str:
        """The record id of a review thread."""
        return thread_record_id(org_id, team_id, channel_id, thread_ts, KIND)

    def find(
        self, org_id: str, team_id: str, channel_id: str, thread_ts: str
    ) -> SlackThread | None:
        """The thread's review row, or ``None`` when no review ran in it yet."""
        try:
            return self._threads.get_scoped(
                self.key(org_id, team_id, channel_id, thread_ts), org_id
            )
        except KeyError:
            return None

    def start(self, row: SlackThread) -> SlackThread | None:
        """Insert a new row; ``None`` if a concurrent dispatcher won the race."""
        try:
            return self._threads.create(row)
        except ValueError:
            return None

    def restart(
        self, old: SlackThread, fresh: SlackThread, now: datetime
    ) -> SlackThread:
        """Replace a finished review's state with the next one (same row id)."""
        return self._threads.put(
            fresh.model_copy(
                update={
                    "id": old.id,
                    "created_at": old.created_at,
                    "updated_at": now,
                    "request_count": old.request_count + 1,
                }
            )
        )
