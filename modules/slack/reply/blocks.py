"""Block Kit for the two buttons of the Ask flow (design 3.12): Stop and Try again.

A message that carries a button is a ``section`` (the same scrubbed text the
message would have had without buttons) followed by one ``actions`` block. The
button ``value`` is the thread row's random ``run_ref`` (an opaque, per-request
reference, never one of our ids); the endpoint checks it against the stored row
and against the message the press came from. Labels are fixed strings. A message
without a button is edited with an empty block list, because Slack keeps the old
blocks of an edited message unless told otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STOP_ACTION = "weaves.stop"
RETRY_ACTION = "weaves.retry"
BUTTON_ACTIONS = frozenset({STOP_ACTION, RETRY_ACTION})
STOP_LABEL = "Stop"
RETRY_LABEL = "Try again"
MAX_SECTION_CHARS = 3000
MAX_RETRIES = 3


@dataclass(frozen=True)
class Button:
    """One button: the action id Slack sends back, its label and its opaque value."""

    action_id: str
    label: str
    value: str


def stop_button(run_ref: str) -> Button | None:
    """The Stop button of a request (``None`` for a row without a reference)."""
    return Button(STOP_ACTION, STOP_LABEL, run_ref) if run_ref else None


def retry_button(run_ref: str, retries: int) -> Button | None:
    """Try again, while the request still has retries left."""
    if not run_ref or retries >= MAX_RETRIES:
        return None
    return Button(RETRY_ACTION, RETRY_LABEL, run_ref)


def message_blocks(text: str, button: Button | None) -> list[dict[str, Any]]:
    """The blocks of a message: its text and, if given, one button.

    ``[]`` (no button) is what clears the buttons of an edited message.
    """
    if button is None:
        return []
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text[:MAX_SECTION_CHARS]},
        },
        {
            "type": "actions",
            "block_id": "weaves.run",
            "elements": [
                {
                    "type": "button",
                    "action_id": button.action_id,
                    "text": {"type": "plain_text", "text": button.label},
                    "value": button.value,
                }
            ],
        },
    ]
