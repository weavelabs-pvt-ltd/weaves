"""At most one neutral "temporarily unavailable" line per channel per hour (3.9)."""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock

WINDOW = timedelta(hours=1)
MAX_KEYS = 2000


class NoticeThrottle:
    """Process-local memory of when each channel last got the unavailable line.

    A restart forgets it, which at worst repeats one short line; it never blocks
    an answer and holds no text.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._last: dict[tuple[str, str], datetime] = {}
        self._lock = Lock()

    def allow(self, team_id: str, channel_id: str, now: datetime) -> bool:
        """True (and remember it) if this channel has not been told within the hour."""
        key = (team_id, channel_id)
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < WINDOW:
                return False
            if len(self._last) >= MAX_KEYS:
                oldest = min(self._last, key=lambda k: self._last[k])
                del self._last[oldest]
            self._last[key] = now
            return True
