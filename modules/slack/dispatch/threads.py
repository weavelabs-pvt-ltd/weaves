"""The ``slack_threads`` collection: one row per Slack thread, latest request state.

The record id is the thread key (organization, team, channel, thread ``ts``,
kind), so the primary key and the unique index of migration 33 agree: two
dispatchers racing to start the same thread cannot both succeed. Reads and writes
run under the organization's tenant context like every other collection.
"""

from __future__ import annotations

from datetime import datetime

from application.ports.repository import Repository
from contracts.v1.slack_threads import SlackThread, thread_record_id


class ThreadBook:
    """Find, start and renew thread rows."""

    def __init__(self, threads: Repository[SlackThread]) -> None:
        """Bind the (tenant-scoped) repository."""
        self._threads = threads

    @staticmethod
    def key(
        org_id: str, team_id: str, channel_id: str, thread_ts: str, kind: str = "ask"
    ) -> str:
        """The record id of a thread (``ask`` by default, or ``task``)."""
        return thread_record_id(org_id, team_id, channel_id, thread_ts, kind)

    def find(
        self,
        org_id: str,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        kind: str = "ask",
    ) -> SlackThread | None:
        """The thread row, or ``None`` when no such request ran in this thread yet."""
        try:
            return self._threads.get_scoped(
                self.key(org_id, team_id, channel_id, thread_ts, kind), org_id
            )
        except KeyError:
            return None

    def start(self, row: SlackThread) -> SlackThread | None:
        """Insert a new thread; ``None`` if a concurrent dispatcher won the race."""
        try:
            return self._threads.create(row)
        except ValueError:
            return None

    def save(self, row: SlackThread, now: datetime) -> SlackThread:
        """Write the row back with a fresh ``updated_at``."""
        return self._threads.put(row.model_copy(update={"updated_at": now}))

    def renew(
        self,
        row: SlackThread,
        *,
        turn_id: str,
        event_id: str,
        request_id: str,
        message_ts: str,
        slack_user_id: str,
        now: datetime,
    ) -> SlackThread:
        """Start the next request in a thread (a follow-up): fresh request state."""
        return self.save(
            row.model_copy(
                update={
                    "turn_id": turn_id,
                    "event_id": event_id,
                    "request_id": request_id,
                    "message_ts": message_ts,
                    "requester_slack_user_id": slack_user_id,
                    "requested_at": now,
                    "request_count": row.request_count + 1,
                    "run_state": "queued",
                    "stage": "",
                    "status_ts": None,
                    "status_text_key": "",
                    "answer_ts": None,
                    "reply_state": "pending",
                    "reply_attempts": 0,
                    "next_attempt_at": None,
                    "reply_error": "",
                    "posts": 0,
                    "private_sent": False,
                    "finished_at": None,
                }
            ),
            now,
        )
