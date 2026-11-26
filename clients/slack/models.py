"""Small typed results returned by the Slack client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class MessageRef:
    """Where a posted message lives: needed to edit, delete or react to it."""

    channel: str
    ts: str


@dataclass(frozen=True)
class StatusResult:
    """Outcome of ``assistant.threads.setStatus``; unsupported is not an error."""

    status: Literal["ok", "unsupported", "error"]
    error: str | None = None


@dataclass(frozen=True)
class RepliesPage:
    """One page of thread messages (at most 15 for non-Marketplace apps)."""

    messages: list[dict[str, Any]]
    has_more: bool
    next_cursor: str | None


@dataclass(frozen=True)
class ChannelPage:
    """One page of channels the bot token can list."""

    channels: list[dict[str, Any]]
    next_cursor: str | None
