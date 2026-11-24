"""Resolve reservations whose worker is gone, and apply usage retention.

A reservation is open from before the provider request until the call is
settled or released. If the process dies in between, the row stays ``reserved``.
The reconciler decides by the existing lease fences, never by a wall-clock TTL
of its own (runtime budgets reach 3600 s):

* the run row already carries provider tokens: settle from it (no loss);
* otherwise, once the owning lease is expired, mark the row ``unknown``: the
  request may have reached the provider, the reservation stays as the charged
  upper bound and the row is never double counted. A late real settlement still
  upgrades ``unknown`` to ``settled`` exactly once.

The lease is expired when the workflow run is no longer running, the execution
job that owns it stopped heartbeating (the same 120 s fence job recovery uses),
the run itself passed the run recovery fence, or, for a task turn's run, the turn
stopped heartbeating (the same fence turn recovery uses) or is no longer running.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from application.ports.repository import Repository
from application.ports.usage import UsageLedger, UsageSettlement
from application.runtime_constants import (
    STALE_EXECUTION_JOB_AFTER_SECONDS,
    STALE_RUN_AFTER_SECONDS,
    STALE_TASK_TURN_AFTER_SECONDS,
)
from application.task_run_keys import task_turn_id_from_run_key
from contracts.v1 import (
    AgentRun,
    ExecutionJob,
    ExecutionJobStatus,
    RunStatus,
    TaskTurn,
    TaskTurnStatus,
    UsageEvent,
    WorkflowRun,
)
from contracts.v1.base import ProductContract
from modules.usage.cost import usd_to_nano
from shared.clock import utc_now
from shared.request_context import system_scope

logger = logging.getLogger("product.usage")
_LIVE_JOB = {ExecutionJobStatus.RUNNING, ExecutionJobStatus.CANCEL_REQUESTED}
SCOPE = "usage"


class UsageReconciler:
    """Settle or mark unknown every reservation whose lease has expired."""

    def __init__(
        self,
        *,
        ledger: UsageLedger,
        runs: Repository[WorkflowRun],
        agent_runs: Repository[AgentRun],
        execution_jobs: Repository[ExecutionJob],
        task_turns: Repository[TaskTurn],
        retention_days: int,
        batch: int = 200,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the ledger and the run records it reconciles against."""
        self.ledger = ledger
        self.runs = runs
        self.agent_runs = agent_runs
        self.execution_jobs = execution_jobs
        self.task_turns = task_turns
        self.retention_days = retention_days
        self.batch = batch
        self._clock = clock

    def reconcile(self) -> int:
        """Resolve expired reservations; return how many rows changed."""
        now = self._clock()
        changed = 0
        with system_scope(SCOPE):
            for event in self.ledger.list_reserved_before(now, limit=self.batch):
                try:
                    changed += self._resolve(event, now)
                except Exception:
                    logger.exception(
                        "usage reconcile failed org_id=%s run=%s",
                        event.org_id,
                        event.agent_run_id,
                    )
        return changed

    def purge_expired(self) -> int:
        """Retention for live organizations only (deletion removes the rest)."""
        cutoff = (self._clock() - timedelta(days=self.retention_days)).date()
        with system_scope(SCOPE):
            return self.ledger.purge_before(
                cutoff, self.batch
            ) + self.ledger.purge_deleted_organizations(self.batch)

    def _resolve(self, event: UsageEvent, now: datetime) -> int:
        agent_run = self._find(self.agent_runs, event.agent_run_id, event)
        if agent_run is not None and agent_run.input_tokens is not None:
            settled = self.ledger.settle(self._settlement(event, agent_run, now))
            if settled:
                logger.info(
                    "usage settled from run record org_id=%s run=%s",
                    event.org_id,
                    event.agent_run_id,
                )
            return int(settled)
        run = self._find(self.runs, event.run_id, event)
        if run is not None and not self._lease_expired(run, now):
            return 0
        outcome = "lease_expired" if run is not None else "run_missing"
        marked = self.ledger.mark_unknown(
            event.org_id, event.reservation_key, outcome=outcome, now=now
        )
        if marked:
            logger.warning(
                "usage unknown org_id=%s run=%s outcome=%s",
                event.org_id,
                event.agent_run_id,
                outcome,
            )
        return int(marked)

    @staticmethod
    def _find[T: ProductContract](
        repo: Repository[T], record_id: str, event: UsageEvent
    ) -> T | None:
        try:
            return repo.get_scoped(record_id, event.org_id, event.workspace_id)
        except KeyError:
            return None

    @staticmethod
    def _settlement(
        event: UsageEvent, agent_run: AgentRun, now: datetime
    ) -> UsageSettlement:
        cost = agent_run.estimated_cost_usd
        return UsageSettlement(
            org_id=event.org_id,
            workspace_id=event.workspace_id,
            reservation_key=event.reservation_key,
            agent_run_id=event.agent_run_id,
            run_id=event.run_id,
            agent_id=event.agent_id,
            provider_id=agent_run.resolved_provider_id or event.provider_id,
            model_id=agent_run.resolved_model_id or event.model_id,
            input_tokens=agent_run.input_tokens or 0,
            output_tokens=agent_run.output_tokens or 0,
            estimated_cost_nano_usd=None if cost is None else usd_to_nano(cost),
            outcome="reconciled",
            now=now,
        )

    def _lease_expired(self, run: WorkflowRun, now: datetime) -> bool:
        if run.status is not RunStatus.RUNNING:
            return True
        if run.updated_at <= now - timedelta(seconds=STALE_RUN_AFTER_SECONDS):
            return True
        if run.idempotency_key is None:
            return False
        try:
            job = self.execution_jobs.get_scoped(
                run.idempotency_key, run.org_id, run.workspace_id
            )
        except KeyError:
            return self._turn_lease_expired(run, now)
        stale = now - timedelta(seconds=STALE_EXECUTION_JOB_AFTER_SECONDS)
        return job.status not in _LIVE_JOB or job.updated_at <= stale

    def _turn_lease_expired(self, run: WorkflowRun, now: datetime) -> bool:
        """A task turn's run is orphaned once its turn stops heartbeating."""
        turn_id = task_turn_id_from_run_key(run.idempotency_key)
        if turn_id is None:
            return False
        try:
            turn = self.task_turns.get_scoped(turn_id, run.org_id, run.workspace_id)
        except KeyError:
            return False
        stale = now - timedelta(seconds=STALE_TASK_TURN_AFTER_SECONDS)
        return turn.status is not TaskTurnStatus.RUNNING or turn.updated_at <= stale
