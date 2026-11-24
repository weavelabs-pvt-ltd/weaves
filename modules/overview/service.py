"""Home metrics: bounded aggregates scoped to what the caller may see."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any

from application.ports.overview_data import OverviewData
from contracts.v1 import Permission
from shared.clock import utc_now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)

CACHE_SECONDS = 5.0
CACHE_ENTRIES = 256


class OverviewService:
    """Answer Home from aggregates instead of the caller walking history."""

    def __init__(
        self,
        *,
        data: OverviewData,
        authorize: Callable[[str, Permission], None],
        ensure_ready: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the aggregate reader and the authorization/readiness hooks."""
        self._data = data
        self._authorize = authorize
        self._ensure_ready = ensure_ready
        self._monotonic = monotonic
        self._cache: OrderedDict[
            tuple[str, str, str, int], tuple[float, dict[str, Any]]
        ] = OrderedDict()
        self._lock = Lock()

    def overview(self, *, tz_offset_minutes: int = 0) -> dict[str, Any]:
        """Return the caller's Home metrics, cached per caller for a few seconds.

        Requires task visibility. Private tasks of other principals never
        contribute to any number.
        """
        self._ensure_ready()
        principal = effective_principal_id("local-developer")
        self._authorize(principal, Permission.TASKS_VIEW)
        org, workspace = effective_org_id(), effective_workspace_id()
        key = (org, workspace, principal, tz_offset_minutes)
        now = self._monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(key)
                return dict(cached[1])
        generated_at = utc_now()
        body = self._data.snapshot(
            org,
            workspace,
            principal,
            now=generated_at,
            tz_offset_minutes=tz_offset_minutes,
        )
        body["generated_at"] = generated_at.isoformat().replace("+00:00", "Z")
        with self._lock:
            self._cache[key] = (now + CACHE_SECONDS, body)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return dict(body)
