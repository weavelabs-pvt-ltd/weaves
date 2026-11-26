"""A global token bucket for the Events endpoint (applied after the signature).

Only requests that carry a valid Slack signature spend tokens, so a flood of
forged requests cannot starve real delivery. One bucket per API process.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock


class IngestLimiter:
    """``rate`` tokens per second up to ``burst``."""

    def __init__(
        self,
        rate_per_second: float = 100.0,
        burst: float = 200.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start full."""
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._tokens = burst
        self._updated = clock()
        self._lock = Lock()

    def acquire(self) -> bool:
        """Take one token; ``False`` means answer 429."""
        with self._lock:
            now = self._clock()
            self._tokens = min(
                self._burst, self._tokens + (now - self._updated) * self._rate
            )
            self._updated = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True
