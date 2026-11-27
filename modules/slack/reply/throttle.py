"""Notice throttles for the neutral "unavailable" line and the "no model" line.

The unavailable line is at most one per channel per hour (3.9); the "no AI model
is connected" line is at most one per thread per ten minutes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock

WINDOW = timedelta(hours=1)
NO_MODEL_WINDOW = timedelta(minutes=10)
MAX_KEYS = 2000


class NoticeThrottle:
    """Process-local memory of when each channel last got the unavailable line.

    A restart forgets it, which at worst repeats one short line; it never blocks
    an answer and holds no text.
    """

    def __init__(self, window: timedelta = WINDOW) -> None:
        """Start empty; ``window`` is how long one key stays told."""
        self._window = window
        self._last: dict[tuple[str, str, str], datetime] = {}
        self._lock = Lock()

    def allow(
        self, team_id: str, channel_id: str, now: datetime, thread_ts: str = ""
    ) -> bool:
        """True (and remember it) if this key has not been told within the window.

        The key is the channel, or the thread when ``thread_ts`` is given.
        """
        key = (team_id, channel_id, thread_ts)
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._window:
                return False
            if len(self._last) >= MAX_KEYS:
                oldest = min(self._last, key=lambda k: self._last[k])
                del self._last[oldest]
            self._last[key] = now
            return True
