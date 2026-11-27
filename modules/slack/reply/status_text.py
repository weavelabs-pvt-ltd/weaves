"""The working-state vocabulary: which status a running request shows, and when.

A status is a short *key* stored on the thread row (``queued``, ``checking``,
``check:Jira``, ``writing``, ``still:3``, ``longer``) so the reply tick only
edits the message when the key changes. Keys carry connector display names, never
user text. Timings follow design scene G: the queued notice after 10 seconds,
"Still working (N min)…" from two minutes, "taking longer" from five, and the
failure deadline at fifteen minutes (``WEAVES_SLACK_REPLY_DEADLINE``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta

from contracts.v1.slack_threads import SlackThread
from modules.slack import copy

QUEUED_AFTER = timedelta(seconds=10)
STILL_WORKING_AFTER = timedelta(minutes=2)
TAKING_LONGER_AFTER = timedelta(minutes=5)
DEFAULT_DEADLINE = timedelta(minutes=15)
NATIVE_REFRESH = timedelta(seconds=90)  # setStatus lapses after two minutes
_EMOJI = re.compile(r"^:[a-z0-9_+-]+:\s*")
_LABEL_CHARS = re.compile(r"[^A-Za-z0-9 ._+&/()-]")


def safe_label(label: str) -> str:
    """A connector display name reduced to plain, bounded text for a status."""
    return _LABEL_CHARS.sub("", label).strip()[:40] or "sources"


def text_for(key: str) -> str:
    """The status message of a key (fixed copy plus at most a connector name)."""
    if key == "queued":
        return copy.QUEUED
    if key == "longer":
        return copy.TAKING_LONGER
    if key == "writing":
        return copy.WRITING
    if key.startswith("check:"):
        return copy.checking(safe_label(key[6:]))
    if key.startswith("still:") and key[6:].isdigit():
        return copy.still_working(int(key[6:]))
    return copy.CHECKING


def running_label(row: SlackThread) -> str:
    """The plain-words label of the current step (for ``@Weaves status``)."""
    text = _EMOJI.sub("", text_for(row.status_text_key or "checking"))
    return text.rstrip("….") or "working"


def desired_key(
    *,
    age: timedelta,
    running: bool,
    guard_done: bool,
    waiting_for: Sequence[str],
) -> str | None:
    """The status key a request should show now, or ``None`` (show nothing yet).

    ``waiting_for`` are the display names of the approved connectors that have
    not been read yet, in order; when it is empty the answer is being written.
    """
    if not running:
        return "queued" if age >= QUEUED_AFTER else None
    if age >= TAKING_LONGER_AFTER:
        return "longer"
    if age >= STILL_WORKING_AFTER:
        return f"still:{int(age.total_seconds() // 60)}"
    if not guard_done:
        return "checking"
    if waiting_for:
        return f"check:{safe_label(waiting_for[0])}"
    return "writing"
