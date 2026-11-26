"""In-memory state of the fake Slack server: clock, workspaces, faults, records.

Everything time-related goes through ``clock`` and ``sleep`` so tests inject a
fake clock and never wait. The signing secret and every token are made up.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

DEFAULT_SECRET = "fixture-slack-signing-secret"
DEFAULT_TEAM = "T0FAKE001"
DEFAULT_BOT_TOKEN = "xoxb-fixture-bot-token"
DEFAULT_CODE = "fixture-code"
ACK_DEADLINE_SECONDS = 3.0
REPLIES_INTERVAL_SECONDS = 60.0
STATUS_TTL_SECONDS = 120.0
RESPONSE_URL_USES = 5
RESPONSE_URL_WINDOW_SECONDS = 30 * 60.0
DEFAULT_RETRY_SCHEDULE = [0, 1, 5]


def signing_secret() -> bytes:
    """The made-up secret shared by the fake and its test deliveries."""
    return os.environ.get("TENANCY_SLACK_SIGNING_SECRET", DEFAULT_SECRET).encode()


class SlackState:
    """One fake workspace set; ``reset`` restores the seeded defaults."""

    def __init__(self) -> None:
        """Seed the workspaces, then install the default clock and sleeper."""
        self.clock: Callable[[], float] = time.time
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self.transport: httpx.AsyncBaseTransport | None = None
        self.reset()

    def reset(self) -> None:
        """Restore the seeded defaults."""
        self.sequence = 0
        self.calls: list[dict[str, Any]] = []
        self.tokens: dict[str, dict[str, Any]] = {
            DEFAULT_BOT_TOKEN: {"team": DEFAULT_TEAM, "kind": "bot", "revoked": False}
        }
        self.codes: dict[str, dict[str, Any]] = {
            DEFAULT_CODE: {"team": DEFAULT_TEAM, "enterprise": False}
        }
        self.channels: dict[str, dict[str, Any]] = {
            "C0FAKE001": {"id": "C0FAKE001", "name": "general", "is_private": False},
            "C0FAKE002": {"id": "C0FAKE002", "name": "secret", "is_private": True},
        }
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.ephemerals: list[dict[str, Any]] = []
        self.reactions: set[tuple[str, str, str, str]] = set()
        self.statuses: dict[tuple[str, str], dict[str, Any]] = {}
        self.status_mode = "supported"
        self.last_replies: dict[str, float] = {}
        self.response_urls: dict[str, list[float]] = {}
        self.api_fault: dict[str, Any] | None = None
        self.delivery_fault: str | None = None
        self.retry_schedule: list[float] = list(DEFAULT_RETRY_SCHEDULE)
        self.deliveries: list[dict[str, Any]] = []

    def next_ts(self) -> str:
        """A unique, increasing Slack-style message timestamp."""
        self.sequence += 1
        return f"{int(self.clock())}.{self.sequence:06d}"

    def next_id(self, prefix: str) -> str:
        """A unique id with ``prefix``."""
        self.sequence += 1
        return f"{prefix}{self.sequence:06d}"

    def take_api_fault(self, method: str) -> dict[str, Any] | None:
        """Return the active fault for ``method`` and spend one use of it."""
        fault = self.api_fault
        if fault is None or fault.get("method") not in (None, method):
            return None
        count = fault.get("count")
        if count is not None:
            if count <= 0:
                return None
            fault["count"] = count - 1
        return fault

    def configure(self, body: dict[str, Any]) -> None:
        """Apply a control request; unknown keys are rejected by the caller."""
        if "api_fault" in body:
            self.api_fault = _fault(body["api_fault"])
        if "delivery_fault" in body:
            self.delivery_fault = body["delivery_fault"]
        if "status_mode" in body:
            self.status_mode = body["status_mode"]
        if "retry_schedule_seconds" in body:
            self.retry_schedule = [float(x) for x in body["retry_schedule_seconds"]]
        for code, info in body.get("codes", {}).items():
            self.codes[code] = {
                "team": info.get("team", DEFAULT_TEAM),
                "enterprise": bool(info.get("enterprise", False)),
            }
        for channel in body.get("channels", []):
            self.channels[channel["id"]] = {
                "id": channel["id"],
                "name": channel.get("name", channel["id"]),
                "is_private": bool(channel.get("is_private", False)),
            }
        for message in body.get("messages", []):
            self.messages.setdefault(message["channel"], []).append(
                {
                    "ts": message["ts"],
                    "thread_ts": message.get("thread_ts"),
                    "text": message.get("text", ""),
                    "user": message.get("user", "U0HUMAN01"),
                    "bot": False,
                }
            )


CONFIGURE_KEYS = {
    "api_fault",
    "delivery_fault",
    "status_mode",
    "retry_schedule_seconds",
    "codes",
    "channels",
    "messages",
}
API_FAULT_KINDS = {"500", "429", "slow"}
DELIVERY_FAULTS = {"bad_signature", "stale_timestamp", None}


def _fault(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    if raw.get("kind") not in API_FAULT_KINDS:
        raise ValueError("api_fault.kind must be 500, 429 or slow")
    return {
        "kind": raw["kind"],
        "method": raw.get("method"),
        "count": raw.get("count"),
        "retry_after": raw.get("retry_after", 1),
        "delay_seconds": float(raw.get("delay_seconds", 5)),
    }


STATE = SlackState()
