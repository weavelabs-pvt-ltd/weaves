"""The reconciler resolves reservations by lease expiry and run records."""

from __future__ import annotations

from datetime import timedelta

import pytest

from application.runtime_constants import (
    STALE_EXECUTION_JOB_AFTER_SECONDS,
    STALE_RUN_AFTER_SECONDS,
)
from clients.model_providers.gateway import ModelGateway
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    ModelProviderType,
    ModelResponse,
    ModelUsage,
    UsageState,
)
from shared.clock import utc_now
from tests.support.runtime import LocalPlatformRuntime


class SimulatedCrash(BaseException):
    """The process dies here: no ``except Exception`` handler may absorb it."""


class Model:
    def __init__(self, crash_after_response=False):
        self.crash_after_response = crash_after_response

    def complete(self, request, binding):
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content="reply",
            usage=ModelUsage(input_tokens=21, output_tokens=8, total_tokens=29),
        )


def runtime_with_model():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    runtime.model_adapters[ModelProviderType.LOCAL] = Model()
    runtime.agent_execution_service.model_gateway = ModelGateway(runtime.model_adapters)
    return runtime


def crash_before_write(runtime):
    """Provider answered, then the worker died before the ledger write."""

    def dies(*args, **kwargs):
        raise SimulatedCrash

    runtime.usage_service.settle = dies
    runtime.usage_ledger.settle = dies


def restart(runtime):
    """The worker is back: the real ledger and service are in use again."""
    del runtime.usage_service.settle
    del runtime.usage_ledger.settle


def reconcile_at(runtime, moment):
    runtime.usage_reconciler._clock = lambda: moment
    return runtime.usage_reconciler.reconcile()


def only_row(runtime):
    (row,) = runtime.usage_ledger._rows.values()
    return row


def test_crash_between_response_and_write_is_unknown_after_lease_expiry():
    runtime = runtime_with_model()
    crash_before_write(runtime)
    with pytest.raises(SimulatedCrash):
        runtime.run_agent("hello")
    assert only_row(runtime).state is UsageState.RESERVED
    # The run's lease is still live: nothing is guessed.
    assert reconcile_at(runtime, utc_now()) == 0
    assert only_row(runtime).state is UsageState.RESERVED
    # Past the run recovery fence the request may have reached the provider.
    later = utc_now() + timedelta(seconds=STALE_RUN_AFTER_SECONDS + 5)
    assert reconcile_at(runtime, later) == 1
    row = only_row(runtime)
    assert row.state is UsageState.UNKNOWN and row.outcome == "lease_expired"
    assert reconcile_at(runtime, later) == 0  # nothing left to do
    # A late real settlement upgrades the row once and never double counts.
    restart(runtime)
    assert runtime.usage_ledger.settle(_late(row, later)) is True
    assert runtime.usage_ledger.settle(_late(row, later)) is False
    assert only_row(runtime).state is UsageState.SETTLED


def _late(row, now):
    from application.ports.usage import UsageSettlement

    return UsageSettlement(
        org_id=row.org_id,
        workspace_id=row.workspace_id,
        reservation_key=row.reservation_key,
        agent_run_id=row.agent_run_id,
        run_id=row.run_id,
        agent_id=row.agent_id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        input_tokens=21,
        output_tokens=8,
        estimated_cost_nano_usd=None,
        outcome="late",
        now=now,
    )


def test_a_failed_ledger_write_is_repaired_from_the_run_record():
    runtime = runtime_with_model()

    def blip(*args, **kwargs):
        raise RuntimeError("database blip")

    runtime.usage_ledger.settle = blip
    result = runtime.run_agent("hello")  # the run itself still succeeds
    assert only_row(runtime).state is UsageState.RESERVED
    del runtime.usage_ledger.settle  # the database is back
    assert reconcile_at(runtime, utc_now()) == 1
    row = only_row(runtime)
    assert row.state is UsageState.SETTLED and row.outcome == "reconciled"
    assert (row.input_tokens, row.output_tokens) == (21, 8)
    assert row.agent_run_id == result.agent_run.agent_run_id
    assert reconcile_at(runtime, utc_now()) == 0


def test_an_expired_job_lease_resolves_before_the_run_fence():
    runtime = runtime_with_model()
    job, _ = runtime.enqueue_agent_run("queued work")
    now = utc_now()
    live = ExecutionJob.model_validate(
        job.model_copy(
            update={
                "status": ExecutionJobStatus.RUNNING,
                "attempts": 1,
                "worker_id": "worker-1",
                "started_at": now,
                "updated_at": now,
            }
        ).model_dump()
    )
    runtime.execution_jobs.put(live)
    crash_before_write(runtime)
    with pytest.raises(SimulatedCrash):
        runtime.run_agent("queued work", idempotency_key=job.job_id)
    # A heartbeating job keeps its reservation open.
    assert reconcile_at(runtime, now + timedelta(seconds=30)) == 0
    stale = now + timedelta(seconds=STALE_EXECUTION_JOB_AFTER_SECONDS + 5)
    assert reconcile_at(runtime, stale) == 1
    assert only_row(runtime).state is UsageState.UNKNOWN


def test_a_run_failed_by_recovery_resolves_its_reservation_at_once():
    runtime = runtime_with_model()
    crash_before_write(runtime)
    with pytest.raises(SimulatedCrash):
        runtime.run_agent("hello")
    later = utc_now() + timedelta(seconds=STALE_RUN_AFTER_SECONDS + 5)
    runtime.recovery_service.recover_stale_runs(now=later)  # run recovery tick
    assert only_row(runtime).state is UsageState.RESERVED
    assert reconcile_at(runtime, later) == 1
    assert only_row(runtime).state is UsageState.UNKNOWN


def test_retention_applies_to_live_organizations_only_through_the_tick():
    runtime = runtime_with_model()
    runtime.run_agent("hello")
    assert runtime.usage_reconciler.purge_expired() == 0
    runtime.usage_reconciler._clock = lambda: (
        utc_now() + timedelta(days=runtime.usage_settings.retention_days + 2)
    )
    assert runtime.usage_reconciler.purge_expired() == 2  # row and rollup
    assert runtime.usage_ledger._rows == {}
