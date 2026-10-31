"""Login rate-limiting contract shared by auth use cases and adapters."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class LoginThrottleDecision:
    """Result of consuming a login attempt bucket."""

    allowed: bool
    retry_after_seconds: int = 0


class LoginRateLimiter(Protocol):
    """Consume or clear a hashed login attempt bucket."""

    def consume(self, bucket_key: str, now: datetime) -> LoginThrottleDecision:
        """Consume one attempt and report whether it remains within budget."""
        ...

    def clear(self, bucket_key: str) -> None:
        """Clear all attempt state for the bucket after successful auth."""
        ...
