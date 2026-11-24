"""Atomic admission: count the stored rows under a lock, then insert.

Callers invoke ``admit_*`` inside the same transaction that inserts the new
row. The advisory lock ``limit:{org}:{resource}`` serializes concurrent
admissions of one organization and resource, the count reads the source of
truth, and the lock releases when the transaction ends. There is no counter to
leak on a crash, and a rolled-back or finished row stops counting by itself.

``WEAVES_LIMITS_MODE`` decides what a breach does: ``off`` skips every check,
``observe`` logs the would-be refusal and admits, ``enforce`` raises
``LimitExceeded``. Idempotent replays must be resolved by the caller before
admission is consulted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from application.ports.admission import (
    AGENTS,
    COUNTED_RESOURCES,
    QUEUED_WORK,
    SCHEDULES,
    SEATS,
    TASKS,
    TOOL_CALLS_PER_DAY,
    WORKFLOWS,
    AdmissionCounts,
)
from application.ports.limits import LimitPolicy, OrgLimits
from contracts.v1.limits import LimitUsageView, OrganizationLimits
from modules.limits.errors import LimitExceeded
from modules.platform_controls.controls import PlatformControls

logger = logging.getLogger("product.limits")

QUEUE_RETRY_AFTER_SECONDS = 30
DAY_RETRY_AFTER_SECONDS = 3600


def limit_for(limits: OrgLimits, resource: str) -> int:
    """Return the cap that applies to a counted resource."""
    return {
        QUEUED_WORK: limits.max_queued_per_org,
        TASKS: limits.storage_tasks,
        SEATS: limits.seats,
        SCHEDULES: limits.schedules,
        AGENTS: limits.agents,
        WORKFLOWS: limits.workflows,
        TOOL_CALLS_PER_DAY: limits.tool_calls_per_day,
    }[resource]


def _retry_after(resource: str) -> int | None:
    if resource == QUEUED_WORK:
        return QUEUE_RETRY_AFTER_SECONDS
    if resource == TOOL_CALLS_PER_DAY:
        return DAY_RETRY_AFTER_SECONDS
    return None


class AdmissionService:
    """Admit new rows against the organization's limits."""

    def __init__(
        self,
        policy: LimitPolicy,
        counts: AdmissionCounts,
        lock: Callable[[str], None] | None = None,
        controls: PlatformControls | None = None,
    ) -> None:
        """Bind limits, counting, the advisory lock and the runtime switches."""
        self.policy = policy
        self._counts = counts
        self._lock = lock
        self._controls = controls
        self.controls = controls

    def _paused(self, limits: OrgLimits) -> bool:
        """The effective pause: the runtime switch over the environment default."""
        if self._controls is None:
            return limits.admissions_paused
        return self._controls.snapshot().admissions_paused

    def _refuse_if_forced_pause(self, org_id: str) -> None:
        """An operator's explicit pause refuses work in every limits mode."""
        if self._controls is not None and self._controls.snapshot().admissions_forced:
            logger.warning("admission refused: platform pause org_id=%s", org_id)
            raise LimitExceeded(
                "admissions_paused",
                retry_after_seconds=QUEUE_RETRY_AFTER_SECONDS,
                message="New work is temporarily paused.",
            )

    def _refuse(
        self,
        limits: OrgLimits,
        org_id: str,
        resource: str,
        *,
        limit: int | None,
        used: int | None,
        retry_after: int | None,
        message: str | None = None,
    ) -> None:
        """Raise in ``enforce``; in ``observe`` log the would-be refusal."""
        if limits.enforcing:
            raise LimitExceeded(
                resource,
                limit=limit,
                used=used,
                retry_after_seconds=retry_after,
                message=message,
            )
        logger.warning(
            "limit would be exceeded (observe mode) org_id=%s resource=%s "
            "limit=%s used=%s",
            org_id,
            resource,
            limit,
            used,
        )

    def admit(self, org_id: str, resource: str) -> None:
        """Admit one more ``resource`` row; call inside the inserting transaction."""
        limits = self.policy.for_org(org_id)
        if not limits.active:
            return
        if self._lock is not None:
            self._lock(f"limit:{org_id}:{resource}")
        limit = limit_for(limits, resource)
        used = self._counts.count(org_id, resource)
        if used >= limit:
            self._refuse(
                limits,
                org_id,
                resource,
                limit=limit,
                used=used,
                retry_after=_retry_after(resource),
            )

    def admit_work(self, org_id: str) -> None:
        """Admit one more queued turn or job (pause, global brake, org cap)."""
        self._refuse_if_forced_pause(org_id)
        limits = self.policy.for_org(org_id)
        if not limits.active:
            return
        if self._paused(limits):
            self._refuse(
                limits,
                org_id,
                "admissions_paused",
                limit=None,
                used=None,
                retry_after=QUEUE_RETRY_AFTER_SECONDS,
                message="New work is temporarily paused.",
            )
        elif self._counts.global_queued() >= limits.global_max_queued:
            self._refuse(
                limits,
                org_id,
                "service_capacity",
                limit=None,
                used=None,
                retry_after=QUEUE_RETRY_AFTER_SECONDS,
                message="The service is at capacity; try again shortly.",
            )
        self.admit(org_id, QUEUED_WORK)

    def admit_task(self, org_id: str) -> None:
        """Admit a new task aggregate: its storage share, then its first turn.

        Locks are always taken in this order so concurrent admissions cannot
        deadlock.
        """
        self.admit_work(org_id)
        self.admit(org_id, TASKS)

    def admit_tool_call(self, org_id: str) -> None:
        """Admit one more tool call within today's budget.

        Tool calls are admitted without a lock: the count may overshoot by the
        calls already in flight, which is acceptable for a daily budget.
        """
        limits = self.policy.for_org(org_id)
        if not limits.active:
            return
        used = self._counts.count(org_id, TOOL_CALLS_PER_DAY)
        if used >= limits.tool_calls_per_day:
            self._refuse(
                limits,
                org_id,
                TOOL_CALLS_PER_DAY,
                limit=limits.tool_calls_per_day,
                used=used,
                retry_after=DAY_RETRY_AFTER_SECONDS,
            )

    def usage(self, org_id: str) -> OrganizationLimits:
        """Return the organization's limits and consumption (its own data only)."""
        limits = self.policy.for_org(org_id)
        views = tuple(
            LimitUsageView(
                resource=resource,  # type: ignore[arg-type]
                limit=limit_for(limits, resource),
                used=self._counts.count(org_id, resource),
            )
            for resource in COUNTED_RESOURCES
        )
        return OrganizationLimits(
            mode=limits.mode,
            admissions_paused=self._paused(limits),
            requests_per_minute=limits.requests_per_minute_org,
            limits=views,
        )


class NullAdmission(AdmissionService):
    """Admits everything; the default for services built without limits."""

    def __init__(self) -> None:
        """Skip policy, counting and locking entirely."""

    def admit(self, org_id: str, resource: str) -> None:
        """Admit without counting."""

    def admit_work(self, org_id: str) -> None:
        """Admit without counting."""

    def admit_task(self, org_id: str) -> None:
        """Admit without counting."""

    def admit_tool_call(self, org_id: str) -> None:
        """Admit without counting."""
