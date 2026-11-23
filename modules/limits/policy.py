"""Limit policy that layers operator overrides over the environment defaults."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from threading import Lock

from application.ports.limits import LimitPolicy, OrgLimits
from contracts.v1.limits import OrganizationLimitOverride

logger = logging.getLogger("product.limits")

# Fields an operator may override per organization. Mode, the pause switch and
# the global brake stay process-wide.
OVERRIDABLE_FIELDS: tuple[str, ...] = (
    "max_queued_per_org",
    "requests_per_minute_org",
    "requests_per_minute_principal",
    "request_burst",
    "seats",
    "schedules",
    "agents",
    "workflows",
    "tool_calls_per_day",
    "storage_tasks",
)


def apply_override(base: OrgLimits, override: OrganizationLimitOverride) -> OrgLimits:
    """Return ``base`` with every field the override sets replaced."""
    changes = {
        name: value
        for name in OVERRIDABLE_FIELDS
        if (value := getattr(override, name)) is not None
    }
    return replace(base, **changes)


class OverridingLimitPolicy:
    """Resolve limits from the base policy plus a short-lived cached override.

    ``load`` reads one organization's override row under that organization's
    own identity (it returns None when there is none). A lookup failure never
    blocks traffic: it logs and serves the defaults. The cache keeps request
    middleware from reading the database per request; an operator change shows
    up after ``ttl_seconds``.
    """

    def __init__(
        self,
        base: LimitPolicy,
        load: Callable[[str], OrganizationLimitOverride | None],
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the defaults, the override reader and the cache lifetime."""
        self.base = base
        self._load = load
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = Lock()
        self._cache: dict[str, tuple[float, OrgLimits]] = {}

    def cached(self, org_id: str) -> OrgLimits | None:
        """Return a fresh cached value without touching storage, else None."""
        with self._lock:
            entry = self._cache.get(org_id)
        if entry is not None and self._clock() - entry[0] < self._ttl:
            return entry[1]
        return None

    def for_org(self, org_id: str) -> OrgLimits:
        """Return the effective limits for ``org_id``."""
        hit = self.cached(org_id)
        if hit is not None:
            return hit
        base = self.base.for_org(org_id)
        limits = base
        if base.active:
            try:
                override = self._load(org_id)
            except Exception:
                logger.exception("limit override lookup failed; using defaults")
                override = None
            if override is not None:
                limits = apply_override(base, override)
        with self._lock:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[org_id] = (self._clock(), limits)
        return limits

    def forget(self, org_id: str) -> None:
        """Drop one cached entry (used after an override is written in-process)."""
        with self._lock:
            self._cache.pop(org_id, None)
