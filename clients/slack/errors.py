"""Slack failures. Messages carry only a code, never a token or a body."""

from __future__ import annotations

from typing import Literal

SignatureReason = Literal["missing_header", "stale", "bad_signature", "malformed"]
TOKEN_ERROR_CODES = frozenset({"invalid_auth", "token_revoked", "account_inactive"})


class SlackSignatureError(Exception):
    """An inbound request failed verification; ``reason`` says why."""

    def __init__(self, reason: SignatureReason) -> None:
        """Keep the reason as the whole message."""
        super().__init__(reason)
        self.reason: SignatureReason = reason


class SlackApiError(Exception):
    """Slack answered ``ok: false`` (or something unusable) for a method."""

    def __init__(self, code: str, *, method: str = "") -> None:
        """Keep Slack's error code and the method name."""
        super().__init__(f"slack {method}: {code}" if method else f"slack: {code}")
        self.code = code
        self.method = method


class SlackTokenError(SlackApiError):
    """The bot token is no longer usable (revoked, inactive or invalid)."""


class SlackRateLimited(SlackApiError):
    """Slack asked us to slow down; ``retry_after`` is in seconds."""

    def __init__(self, retry_after: float, *, method: str = "") -> None:
        """Keep the seconds Slack asked us to wait."""
        super().__init__("ratelimited", method=method)
        self.retry_after = retry_after
