"""Per-organization and per-principal request rate limiting (token buckets).

The middleware sits inside authentication, so the tenant identity is already
bound when it runs; requests without one (account routes, health, webhooks) are
not limited here. Buckets live in this API process: with N API replicas the
effective ceiling is N times the configured rate, which is acceptable for the
single-VM deployment this slice targets.

``WEAVES_LIMITS_MODE`` applies: ``off`` skips, ``observe`` logs the would-be
refusal (at most once a minute per bucket) and serves the request, ``enforce``
answers 429 ``limit_exceeded`` with ``Retry-After``.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from application.ports.limits import LimitPolicy, OrgLimits
from apps.api.limit_errors import limit_response
from modules.limits.errors import LimitExceeded
from shared.request_context import current_request_identity

logger = logging.getLogger("product.limits")

_MAX_BUCKETS = 20_000
_IDLE_SECONDS = 600.0
_LOG_EVERY_SECONDS = 60.0


@dataclass
class _Bucket:
    tokens: float
    updated: float
    logged: float = 0.0


class RequestRateLimiter:
    """Two token buckets per request: the organization's and the principal's."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with no buckets."""
        self._clock = clock
        self._lock = Lock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def _refill(
        self, key: tuple[str, str], per_minute: int, burst: int, now: float
    ) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(burst), updated=now)
            self._buckets[key] = bucket
            return bucket
        gained = (now - bucket.updated) * per_minute / 60.0
        bucket.tokens = min(float(burst), bucket.tokens + gained)
        bucket.updated = now
        return bucket

    def acquire(self, org_id: str, principal_id: str, limits: OrgLimits) -> int | None:
        """Take one token from both buckets; return seconds to wait if empty."""
        now = self._clock()
        org_key = ("org", org_id)
        principal_key = ("principal", f"{org_id}:{principal_id}")
        with self._lock:
            if len(self._buckets) > _MAX_BUCKETS:
                self._prune(now)
            org = self._refill(
                org_key, limits.requests_per_minute_org, limits.request_burst, now
            )
            mine = self._refill(
                principal_key,
                limits.requests_per_minute_principal,
                limits.request_burst,
                now,
            )
            waits = [
                (1.0 - bucket.tokens) * 60.0 / rate
                for bucket, rate in (
                    (org, limits.requests_per_minute_org),
                    (mine, limits.requests_per_minute_principal),
                )
                if bucket.tokens < 1.0
            ]
            if not waits:
                org.tokens -= 1.0
                mine.tokens -= 1.0
                return None
            return max(1, math.ceil(max(waits)))

    def should_log(self, org_id: str, principal_id: str) -> bool:
        """Throttle the observe-mode log line to once a minute per principal."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(("principal", f"{org_id}:{principal_id}"))
            if bucket is None or now - bucket.logged < _LOG_EVERY_SECONDS:
                return False
            bucket.logged = now
            return True

    def _prune(self, now: float) -> None:
        stale = [k for k, b in self._buckets.items() if now - b.updated > _IDLE_SECONDS]
        for key in stale:
            del self._buckets[key]
        if len(self._buckets) > _MAX_BUCKETS:
            self._buckets.clear()


async def _limits_for(policy: LimitPolicy, org_id: str) -> OrgLimits:
    """Resolve limits without blocking the event loop on a cache miss."""
    cached = getattr(policy, "cached", None)
    if cached is not None:
        hit = cached(org_id)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        return await run_in_threadpool(policy.for_org, org_id)
    return policy.for_org(org_id)


def install_request_limits(app: FastAPI) -> None:
    """Install the rate limiter; install it before authentication."""
    limiter = RequestRateLimiter()
    app.state.request_rate_limiter = limiter

    @app.middleware("http")
    async def limit_requests(request: Any, call_next: Any) -> Any:
        identity = current_request_identity()
        policy: LimitPolicy | None = getattr(request.app.state, "limit_policy", None)
        if identity is None or policy is None or request.method == "OPTIONS":
            return await call_next(request)
        try:
            limits = await _limits_for(policy, identity.org_id)
        except Exception:
            logger.exception("request limit lookup failed; serving request")
            return await call_next(request)
        if not limits.active:
            return await call_next(request)
        wait = limiter.acquire(identity.org_id, identity.principal_id, limits)
        if wait is None:
            return await call_next(request)
        if limits.enforcing:
            return limit_response(
                LimitExceeded(
                    "requests",
                    limit=limits.requests_per_minute_org,
                    retry_after_seconds=wait,
                    message="Too many requests; slow down and retry shortly.",
                )
            )
        if limiter.should_log(identity.org_id, identity.principal_id):
            logger.warning(
                "request rate would be limited (observe mode) org_id=%s wait=%ss",
                identity.org_id,
                wait,
            )
        return await call_next(request)
