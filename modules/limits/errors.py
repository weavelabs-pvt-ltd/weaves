"""The refusal raised when an organization is at one of its limits."""

from __future__ import annotations

CODE = "limit_exceeded"


class LimitExceeded(Exception):
    """Admission was refused because ``resource`` is at its limit.

    ``limit`` and ``used`` describe only the caller's own organization. They are
    None for global switches (pause, global queue brake), which must not reveal
    other tenants' activity. ``retry_after_seconds`` is set only when waiting
    can help; limits that need a person to delete something leave it None.
    """

    code = CODE
    retryable = False

    def __init__(
        self,
        resource: str,
        *,
        limit: int | None = None,
        used: int | None = None,
        retry_after_seconds: int | None = None,
        message: str | None = None,
    ) -> None:
        """Describe the refused resource without any other tenant's data."""
        text = message or f"limit reached for {resource}"
        super().__init__(text)
        self.resource = resource
        self.limit = limit
        self.used = used
        self.retry_after_seconds = retry_after_seconds
        self.message = text
