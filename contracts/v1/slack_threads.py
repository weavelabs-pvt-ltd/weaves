"""The Slack thread to hidden Q&A task map and the state of its latest request.

One row per Slack thread and kind (unique in the database, migration 33). The
row keeps the thread level facts (which hidden task holds the conversation, who
asked first) and the state of the *latest* request in the thread: follow-ups
need a new mention, a new request starts only after the previous one has been
answered, so one set of request fields is enough.

Nothing here holds Slack message text, an answer, a token or connector data;
only ids, timestamps and short state codes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from contracts.v1.base import MutableOrgScopedContract, OpaqueId

ThreadKind = Literal["ask", "task"]
RunState = Literal["queued", "running", "cancel_requested", "done", "failed", "stopped"]
ReplyState = Literal["pending", "posted", "undeliverable"]

TERMINAL_RUN_STATES: frozenset[str] = frozenset({"done", "failed", "stopped"})


def thread_record_id(
    org_id: str, team_id: str, channel_id: str, thread_ts: str, kind: str
) -> str:
    """The stable record id of a Slack thread (also its unique key)."""
    return f"{org_id}:{team_id}:{channel_id}:{thread_ts}:{kind}"


class SlackThread(MutableOrgScopedContract):
    """A Slack thread bound to its hidden task and the latest request's state."""

    id: OpaqueId
    workspace_id: OpaqueId
    team_id: OpaqueId
    channel_id: OpaqueId
    thread_ts: str = Field(min_length=1, max_length=32)
    kind: ThreadKind = "ask"
    task_id: OpaqueId
    tier: Literal["ask", "execute"] = "ask"
    requester_slack_user_id: OpaqueId
    requester_principal_id: OpaqueId
    # The latest request in the thread.
    turn_id: OpaqueId
    event_id: OpaqueId
    request_id: OpaqueId
    message_ts: str = Field(min_length=1, max_length=32)
    requested_at: AwareDatetime
    request_count: int = Field(default=1, ge=1, le=100_000)
    run_state: RunState = "queued"
    stage: str = Field(default="", max_length=40)
    status_ts: str | None = Field(default=None, max_length=32)
    status_text_key: str = Field(default="", max_length=80)
    answer_ts: str | None = Field(default=None, max_length=32)
    reply_state: ReplyState = "pending"
    reply_attempts: int = Field(default=0, ge=0, le=100)
    next_attempt_at: AwareDatetime | None = None
    reply_error: str = Field(default="", max_length=64)
    # Bot posts made for this request (the design allows two) and whether the
    # one private message of the request was already sent.
    posts: int = Field(default=0, ge=0, le=10)
    private_sent: bool = False
    finished_at: AwareDatetime | None = None


__all__ = [
    "TERMINAL_RUN_STATES",
    "ReplyState",
    "RunState",
    "SlackThread",
    "ThreadKind",
    "thread_record_id",
]
