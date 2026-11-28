"""Read one Slack ``block_actions`` payload into a bounded, validated press.

The endpoint verified the signature over the raw form body before this runs; the
payload is still untrusted data. Only our two buttons are handled: a press on any
other button (the URL buttons of later slices, for example), or any other payload
type, is acked and ignored. Every id and timestamp is matched against a strict
pattern before it is used, so nothing odd reaches a query, a log line or Slack.

Dedupe key (design 3.12): ``ia:{team}:{user}:{action}:{message_ts}:{action_ts}``.
Slack's ``action_ts`` should always be present; when it is not (UNVERIFIED) the
key falls back to a two-second bucket of the receive time, so a double-click
inside one bucket is one press and a deliberate second press later is another.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from modules.slack.reply.blocks import BUTTON_ACTIONS

ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")
VALUE = re.compile(r"[A-Za-z0-9_-]{1,64}")
BUCKET_SECONDS = 2


@dataclass(frozen=True)
class Press:
    """One button press: who, which button, on which message of which thread."""

    team_id: str
    user_id: str
    action_id: str
    value: str
    channel_id: str
    message_ts: str
    thread_ts: str | None
    action_ts: str | None

    @property
    def action(self) -> str:
        """``stop`` or ``retry``."""
        return self.action_id.removeprefix("weaves.")


@dataclass(frozen=True)
class Parsed:
    """A press, or why there is none (``ignored`` or ``malformed``)."""

    press: Press | None = None
    outcome: str = "ignored"


def _valid(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _our_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return None
    for action in actions[:10]:
        if isinstance(action, dict) and action.get("action_id") in BUTTON_ACTIONS:
            return action
    return None


def parse_press(payload: dict[str, Any]) -> Parsed:
    """Validate a ``block_actions`` payload for one of our buttons."""
    if payload.get("type") != "block_actions":
        return Parsed()
    action = _our_action(payload)
    if action is None:
        return Parsed()
    container, message = _dict(payload.get("container")), _dict(payload.get("message"))
    team = _valid(_dict(payload.get("team")).get("id"), ID)
    user = _valid(_dict(payload.get("user")).get("id"), ID)
    channel = _valid(
        container.get("channel_id") or _dict(payload.get("channel")).get("id"), ID
    )
    message_ts = _valid(container.get("message_ts") or message.get("ts"), TS)
    value = _valid(action.get("value"), VALUE)
    if not (team and user and channel and message_ts and value):
        return Parsed(outcome="malformed")
    thread = _valid(message.get("thread_ts") or container.get("thread_ts"), TS)
    return Parsed(
        Press(
            team_id=team,
            user_id=user,
            action_id=str(action["action_id"]),
            value=value,
            channel_id=channel,
            message_ts=message_ts,
            thread_ts=thread,
            action_ts=_valid(action.get("action_ts"), TS),
        )
    )


def press_event_id(press: Press, received: datetime) -> str:
    """The inbox key of a press (the dedupe key of design 3.12)."""
    stamp = press.action_ts or f"b{int(received.timestamp()) // BUCKET_SECONDS}"
    return (
        f"ia:{press.team_id}:{press.user_id}:{press.action_id}:"
        f"{press.message_ts}:{stamp}"
    )


def press_request_id(event_id: str) -> str:
    """A short request id for the press (the key itself is longer than allowed)."""
    return "slack-ia-" + hashlib.sha256(event_id.encode()).hexdigest()[:16]
