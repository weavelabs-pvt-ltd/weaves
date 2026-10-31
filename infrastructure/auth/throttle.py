"""Login-attempt throttling shared by the local and PostgreSQL runtimes."""

from __future__ import annotations

import math
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from application.ports.throttling import LoginRateLimiter, LoginThrottleDecision
from shared.errors import LoginThrottled, RateLimitExceeded
from shared.login_buckets import login_bucket_key


class InMemoryLoginRateLimiter:
    """Fixed-window limiter for a single local API process."""

    def __init__(
        self,
        *,
        limit: int = 5,
        window_seconds: int = 900,
        max_buckets: int = 20_000,
    ) -> None:
        if limit < 1 or window_seconds < 1 or max_buckets < 1:
            raise ValueError("login throttle limits must be positive")
        self.limit = limit
        self.window = timedelta(seconds=window_seconds)
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[str, tuple[datetime, int]] = OrderedDict()
        self._lock = Lock()

    def consume(self, bucket_key: str, now: datetime) -> LoginThrottleDecision:
        with self._lock:
            current = self._buckets.get(bucket_key)
            if current is None or current[0] + self.window <= now:
                window_started_at, attempts = now, 1
            else:
                window_started_at = current[0]
                attempts = min(current[1] + 1, self.limit + 1)
            self._buckets[bucket_key] = (window_started_at, attempts)
            self._buckets.move_to_end(bucket_key)
            self._prune(now)
            if attempts <= self.limit:
                return LoginThrottleDecision(allowed=True)
            retry_after = math.ceil(
                (window_started_at + self.window - now).total_seconds()
            )
            return LoginThrottleDecision(
                allowed=False, retry_after_seconds=max(1, retry_after)
            )

    def clear(self, bucket_key: str) -> None:
        with self._lock:
            self._buckets.pop(bucket_key, None)

    def _prune(self, now: datetime) -> None:
        for bucket_key, (window_started_at, _) in tuple(self._buckets.items()):
            if window_started_at + self.window <= now:
                self._buckets.pop(bucket_key, None)
        while len(self._buckets) > self.max_buckets:
            self._buckets.popitem(last=False)


class PostgresLoginRateLimiter:
    """Atomic fixed-window limiter shared by API processes using one database."""

    def __init__(
        self,
        pool: Any,
        *,
        limit: int = 5,
        window_seconds: int = 900,
        prune_every: int = 256,
    ) -> None:
        if limit < 1 or window_seconds < 1 or prune_every < 1:
            raise ValueError("login throttle limits must be positive")
        self._pool = pool
        self.limit = limit
        self.window_seconds = window_seconds
        self._prune_every = prune_every
        self._calls = 0
        self._prune_lock = Lock()

    def consume(self, bucket_key: str, now: datetime) -> LoginThrottleDecision:
        cutoff = now - timedelta(seconds=self.window_seconds)
        with self._pool.connection() as connection:
            row = connection.execute(
                """INSERT INTO product_login_throttle
                       (bucket_key, window_started_at, attempts, updated_at)
                   VALUES (%s, %s, 1, %s)
                   ON CONFLICT (bucket_key) DO UPDATE SET
                       window_started_at = CASE
                           WHEN product_login_throttle.window_started_at <= %s
                           THEN EXCLUDED.window_started_at
                           ELSE product_login_throttle.window_started_at
                       END,
                       attempts = CASE
                           WHEN product_login_throttle.window_started_at <= %s
                           THEN 1
                           ELSE LEAST(product_login_throttle.attempts + 1, %s)
                       END,
                       updated_at = EXCLUDED.updated_at
                   RETURNING attempts, window_started_at""",
                (bucket_key, now, now, cutoff, cutoff, self.limit + 1),
            ).fetchone()
        if row is None:
            raise RuntimeError("login throttle update returned no row")
        attempts, window_started_at = row
        self._maybe_prune(now)
        if attempts <= self.limit:
            return LoginThrottleDecision(allowed=True)
        retry_after = math.ceil(
            (
                window_started_at + timedelta(seconds=self.window_seconds) - now
            ).total_seconds()
        )
        return LoginThrottleDecision(
            allowed=False, retry_after_seconds=max(1, retry_after)
        )

    def clear(self, bucket_key: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM product_login_throttle WHERE bucket_key = %s",
                (bucket_key,),
            )

    def _maybe_prune(self, now: datetime) -> None:
        with self._prune_lock:
            self._calls += 1
            if self._calls % self._prune_every:
                return
        cutoff = now - timedelta(seconds=self.window_seconds * 2)
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM product_login_throttle WHERE window_started_at < %s",
                (cutoff,),
            )


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "InMemoryLoginRateLimiter",
    "LoginRateLimiter",
    "LoginThrottleDecision",
    "LoginThrottled",
    "PostgresLoginRateLimiter",
    "RateLimitExceeded",
    "login_bucket_key",
    "utc_now",
]
