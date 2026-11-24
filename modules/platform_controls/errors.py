"""Refusals caused by a platform-wide switch (never by one tenant's limits)."""

from __future__ import annotations

READ_ONLY_CODE = "platform_read_only"
SIGNUP_CLOSED_CODE = "signup_closed"


class PlatformUnavailable(Exception):
    """A global switch is refusing this request (maintenance, not a tenant fault)."""

    retryable = True

    def __init__(
        self, code: str, message: str, *, retry_after_seconds: int = 30
    ) -> None:
        """Carry the stable code and a message safe to show to any tenant."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds
