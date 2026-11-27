"""Reaction feedback on a Slack answer (design 3.13).

One row per answered request and asker: the latest thumbs up or down the asker
put on our answer message. The row keeps ids, a value and a time only. It never
holds Slack message text, a reason, a raw Slack user id or a token: the asker is
the mapped Weaves member's principal id, or else a per-organization HMAC of the
Slack user id (``asker_kind`` says which).

The ``agent_id``, ``model_id`` and ``connector_set`` fields are a snapshot of the
run taken when the reaction arrives, so the analytics report still works after
the hidden conversation (and its run records) were deleted by retention.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from contracts.v1.base import MutableOrgScopedContract, OpaqueId

FeedbackValue = Literal[1, -1]
FeedbackSource = Literal["slack", "web"]
AskerKind = Literal["member", "hashed"]
MAX_CONNECTOR_SET_CHARS = 1000


def feedback_record_id(org_id: str, turn_id: str, asker_ref: str) -> str:
    """The stable record id (also the unique key) of one asker's rating of a turn."""
    return f"{org_id}:{turn_id}:{asker_ref}"


class SlackFeedback(MutableOrgScopedContract):
    """The asker's latest thumbs up or down on one answer."""

    id: str = Field(min_length=1, max_length=400)
    answer_ref: str = Field(min_length=1, max_length=80)
    turn_id: OpaqueId
    task_id: OpaqueId
    agent_run_id: OpaqueId | None = None
    request_id: OpaqueId | None = None
    asker_ref: OpaqueId
    asker_kind: AskerKind
    value: FeedbackValue
    source: FeedbackSource = "slack"
    at: AwareDatetime
    agent_id: OpaqueId | None = None
    model_id: str | None = Field(default=None, max_length=200)
    connector_set: str = Field(default="", max_length=MAX_CONNECTOR_SET_CHARS)


__all__ = [
    "AskerKind",
    "FeedbackSource",
    "FeedbackValue",
    "SlackFeedback",
    "feedback_record_id",
]
