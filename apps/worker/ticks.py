"""Periodic worker work as a registry; slices add one line, not loop edits.

A ``Tick`` is a named callable run by ``apps.worker.loop.WorkerLoop`` at most
once per ``interval_seconds`` (0 means every loop iteration). Ticks run in
registration order before work is claimed. An exception escaping a tick aborts
that iteration exactly as before (it is logged, the loop waits one poll
interval and tries again, and the tick's timer is not advanced); ticks that must
never disturb the loop catch and log their own failures.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from apps.worker.services import WorkerServices
from shared.metrics import run_collectors

logger = logging.getLogger("product.worker")

WAITLIST_PURGE_SECONDS = 3600
RECOVERY_SWEEP_SECONDS = 86_400
RECOVERY_SECONDS = 30
METRICS_GAUGE_SECONDS = 15
QUEUE_HEALTH_SECONDS = 30
CLAIM_STATE_PRUNE_SECONDS = 3600
USAGE_RECONCILE_SECONDS = 30
USAGE_RETENTION_SECONDS = 3600
SLACK_LIFECYCLE_SECONDS = 600


@dataclass(frozen=True)
class Tick:
    """One periodic job: ``run(services)`` every ``interval_seconds``."""

    name: str
    interval_seconds: float
    run: Callable[[WorkerServices], object]
    # Recovery waits one full interval after start so a restarting worker does
    # not immediately fight leases that are still being heartbeated elsewhere.
    run_at_start: bool = True


class TickRegistry:
    """Ordered, uniquely named ticks."""

    def __init__(self) -> None:
        """Start empty."""
        self._ticks: list[Tick] = []

    def register(self, tick: Tick) -> None:
        """Append ``tick``; names are unique and intervals non-negative."""
        if any(existing.name == tick.name for existing in self._ticks):
            raise ValueError(f"duplicate worker tick {tick.name!r}")
        if tick.interval_seconds < 0:
            raise ValueError("tick interval cannot be negative")
        self._ticks.append(tick)

    @property
    def ticks(self) -> tuple[Tick, ...]:
        """Registered ticks in run order."""
        return tuple(self._ticks)


def _recover_worker_state(services: WorkerServices) -> None:
    """Recover stale task turns and execution records."""
    recovered_task_turns = services.tasks.recover_stale_turns()
    if recovered_task_turns:
        logger.warning("recovered stale task turns count=%d", len(recovered_task_turns))
    recovery = services.execution_recovery
    recovery.recover_stale_runs()
    recovered_jobs = recovery.recover_stale_execution_jobs()
    if recovered_jobs:
        logger.warning("recovered stale execution jobs count=%d", len(recovered_jobs))


def _recovery_sweep(services: WorkerServices) -> None:
    """Complete half-written recovery bookkeeping; never disturb the loop."""
    try:
        services.execution_recovery.recover_stale_runs(sweep=True)
    except Exception:
        logger.exception("recovery sweep failed")


def _purge_account_rows(services: WorkerServices) -> None:
    """Drop a bounded batch of finished account rows; never disturb recovery."""
    if services.purge_accounts is None:
        return
    try:
        purged = services.purge_accounts()
    except Exception:
        logger.exception("account purge failed")
        return
    if purged:
        logger.info("purged finished account rows count=%d", purged)


def _recovery(services: WorkerServices) -> None:
    _recover_worker_state(services)
    _purge_account_rows(services)


def _purge_organizations(services: WorkerServices) -> None:
    """Advance pending organization deletions; never disturb the loop."""
    if services.purge_organizations is None:
        return
    try:
        advanced = services.purge_organizations()
    except Exception:
        logger.exception("organization deletion tick failed")
        return
    if advanced:
        logger.info("organization deletion advanced count=%d", advanced)


def _purge_waitlist(services: WorkerServices) -> None:
    """Drop stale waitlist submissions; failures are logged, never raised."""
    if services.purge_waitlist is None:
        return
    try:
        removed = services.purge_waitlist()
    except Exception:
        logger.exception("waitlist retention tick failed")
        return
    if removed:
        logger.info("waitlist retention removed rows count=%d", removed)


def _reconcile_usage(services: WorkerServices) -> None:
    """Resolve usage reservations whose lease expired; never disturb the loop."""
    if services.reconcile_usage is None:
        return
    try:
        changed = services.reconcile_usage()
    except Exception:
        logger.exception("usage reconciliation tick failed")
        return
    if changed:
        logger.info("usage reservations reconciled count=%d", changed)


def _purge_usage(services: WorkerServices) -> None:
    """Apply usage retention to live organizations; failures are only logged."""
    if services.purge_usage is None:
        return
    try:
        removed = services.purge_usage()
    except Exception:
        logger.exception("usage retention tick failed")
        return
    if removed:
        logger.info("usage retention removed rows count=%d", removed)


def _slack_lifecycle(services: WorkerServices) -> None:
    """Retire half-ended Slack tokens and expire the inbox; never disturb the loop."""
    if services.slack_lifecycle is None:
        return
    try:
        changed = services.slack_lifecycle()
    except Exception:
        logger.exception("slack lifecycle tick failed")
        return
    if changed:
        logger.info("slack lifecycle changed count=%d", changed)


def _dispatch_schedules(services: WorkerServices) -> None:
    scheduled_jobs = services.workflow_automation.dispatch_due_workflow_schedules()
    if scheduled_jobs:
        logger.info("workflow schedules dispatched count=%d", len(scheduled_jobs))


def _studio(services: WorkerServices) -> None:
    paused = getattr(services, "worker_paused", None)
    if services.studio is not None and not (callable(paused) and paused() is True):
        services.studio.tick()


def _metrics_gauges(_: WorkerServices) -> None:
    """Refresh queue gauges; a metrics failure never disturbs the loop."""
    run_collectors()


def _queue_age(services: WorkerServices) -> None:
    """Warn when queued work waits too long; never disturb the loop."""
    if services.queue_health is None:
        return
    try:
        services.queue_health.oldest_queued_age_seconds()
    except Exception:
        logger.exception("queue age check failed")


def _prune_claim_state(services: WorkerServices) -> None:
    """Drop stale round-robin history; failures are logged, never raised."""
    if services.queue_health is None:
        return
    try:
        services.queue_health.prune_claim_state()
    except Exception:
        logger.exception("claim state prune failed")


def build_tick_registry(
    *, schedule_interval: float, lifecycle_interval: float
) -> TickRegistry:
    """The worker's ticks in run order. Phase 5 slices add one line each below."""
    registry = TickRegistry()
    registry.register(Tick("recovery", RECOVERY_SECONDS, _recovery, run_at_start=False))
    registry.register(
        Tick("organization_purge", lifecycle_interval, _purge_organizations)
    )
    registry.register(Tick("waitlist_purge", WAITLIST_PURGE_SECONDS, _purge_waitlist))
    registry.register(Tick("schedule_dispatch", schedule_interval, _dispatch_schedules))
    registry.register(Tick("studio", 0, _studio))

    # Phase 5 registrations. Each slice adds exactly one line in its own block.

    # 5.2 fair claiming: supervisor ticks (slots execute, these keep running).
    registry.register(Tick("queue_age", QUEUE_HEALTH_SECONDS, _queue_age))
    registry.register(
        Tick("claim_state_prune", CLAIM_STATE_PRUNE_SECONDS, _prune_claim_state)
    )

    # 5.3 usage ledger: reservation recovery (lease expiry) and retention purge.
    registry.register(
        Tick("usage_reconcile", USAGE_RECONCILE_SECONDS, _reconcile_usage)
    )
    registry.register(Tick("usage_retention", USAGE_RETENTION_SECONDS, _purge_usage))

    # 5.4 bounded queries: the daily full sweep behind the indexed recovery tick.
    registry.register(
        Tick("recovery_sweep", RECOVERY_SWEEP_SECONDS, _recovery_sweep, False)
    )

    # 5.5 metrics: periodic gauges (queue depth, oldest queued age).
    registry.register(Tick("metrics_gauges", METRICS_GAUGE_SECONDS, _metrics_gauges))

    # S7 Slack lifecycle: unfinished token retirement sweep and inbox retention.
    registry.register(
        Tick("slack_lifecycle", SLACK_LIFECYCLE_SECONDS, _slack_lifecycle)
    )

    return registry
