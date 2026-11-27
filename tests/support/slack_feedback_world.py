"""Helpers for the reaction feedback tests: answered threads and stored reactions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from application.ports.slack import InboxEvent
from contracts.v1.slack_access import SlackAllowedUser
from tests.support.slack_ask_world import BOT_USER, AskWorld
from tests.support.slack_pipeline_world import CHANNEL, ORG, TEAM, USER

__all__ = [
    "BOT_USER",
    "CHANNEL",
    "ORG",
    "TEAM",
    "USER",
    "answered",
    "feedback",
    "react",
]


def answered(aworld: AskWorld, text: str = "what is idempotency?") -> Any:
    """Ask once and return the thread row whose answer was posted."""
    aworld.ask(text)
    row = aworld.latest()
    assert row.answer_ts and row.reply_state == "posted"
    return row


def react(
    aworld: AskWorld,
    ts: str | None,
    *,
    reaction: str = "+1",
    user: str = USER,
    channel: str = CHANNEL,
    event_id: str | None = None,
    received_at: datetime | None = None,
) -> InboxEvent:
    """Store a ``reaction_added`` row as the events endpoint would."""
    aworld.serial += 1
    ident = event_id or f"EvR{aworld.serial:06d}"
    event = InboxEvent(
        event_id=ident,
        team_id=TEAM,
        event_type="reaction_added",
        received_at=received_at or datetime.now(UTC),
        org_id=ORG,
        installation_id="slack-inst-1",
        request_id=f"slack-{ident}",
        channel_id=channel,
        user_id=user,
        message_ts=ts,
        reaction=reaction,
    )
    assert aworld.runtime.slack_store.record_event(event)
    return event


def feedback(aworld: AskWorld) -> list[Any]:
    """Every stored rating of the organization."""
    return list(aworld.runtime.slack_feedback.list())


def inbox_state(aworld: AskWorld, event: InboxEvent) -> tuple[str, str | None]:
    """(state, drop reason) of one stored reaction row."""
    (row,) = [e for e in aworld.inbox() if e.event_id == event.event_id]
    return row.state, row.drop_reason


def map_member(aworld: AskWorld, slack_user_id: str, principal_id: str) -> None:
    """Put the Slack user on the allowlist, mapped to a Weaves member."""
    now = datetime.now(UTC)
    aworld.runtime.slack_allowed_users.put(
        SlackAllowedUser(
            id=f"{ORG}:{slack_user_id}",
            org_id=ORG,
            slack_user_id=slack_user_id,
            principal_id=principal_id,
            added_by=principal_id,
            created_at=now,
            updated_at=now,
        )
    )
