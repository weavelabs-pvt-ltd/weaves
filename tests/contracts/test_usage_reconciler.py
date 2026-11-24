"""The reconciler resolves reservations by lease expiry and run records."""

from __future__ import annotations

from datetime import timedelta

import pytest

from application.runtime_constants import (
    STALE_EXECUTION_JOB_AFTER_SECONDS,
    STALE_RUN_AFTER_SECONDS,
    STALE_TASK_TURN_AFTER_SECONDS,
)
from application.task_run_keys import task_run_key, task_turn_id_from_run_key
from clients.model_providers.gateway import ModelGateway
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    ModelProviderType,
    ModelResponse,
    ModelUsage,
    TaskTurnStatus,
    UsageGroupBy,
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


def killed_task_turn(runtime):
    """A task turn whose worker died inside the model call; returns its turn."""
    task = runtime.task_service.create_task(
        agent_id="local-assistant", title="Killed", initial_message="hello"
    )
    crash_before_write(runtime)
    with pytest.raises(SimulatedCrash):
        runtime.task_worker_service.process_next_turn("worker-killed")
    (turn,) = runtime.task_turns.list_scoped(task.org_id, task.workspace_id)
    assert turn.status is TaskTurnStatus.RUNNING
    assert only_row(runtime).state is UsageState.RESERVED
    return turn


def tokens_counted(runtime, row):
    day = utc_now().date()
    totals = runtime.usage_ledger.summary(row.org_id, day, day, UsageGroupBy.DAY)[0]
    return totals.total_tokens


def test_run_key_round_trips_to_its_turn():
    assert task_turn_id_from_run_key(task_run_key("turn-1", 2)) == "turn-1"
    assert task_turn_id_from_run_key("job-1") is None
    assert task_turn_id_from_run_key(None) is None
    assert task_turn_id_from_run_key("task::run:1") is None


def test_killed_task_turn_is_unknown_at_the_turn_lease_not_after_four_hours():
    runtime = runtime_with_model()
    turn = killed_task_turn(runtime)
    # The worker is gone but the lease is still inside its fence: nothing is guessed.
    soon = turn.updated_at + timedelta(seconds=STALE_TASK_TURN_AFTER_SECONDS - 10)
    assert reconcile_at(runtime, soon) == 0
    assert only_row(runtime).state is UsageState.RESERVED
    expired = turn.updated_at + timedelta(seconds=STALE_TASK_TURN_AFTER_SECONDS + 5)
    assert expired < turn.updated_at + timedelta(seconds=STALE_RUN_AFTER_SECONDS)
    assert reconcile_at(runtime, expired) == 1
    row = only_row(runtime)
    assert row.state is UsageState.UNKNOWN and row.outcome == "lease_expired"
    assert tokens_counted(runtime, row) == 0
    assert reconcile_at(runtime, expired) == 0  # idempotent
    # A late real settlement still upgrades the row exactly once.
    restart(runtime)
    assert runtime.usage_ledger.settle(_late(row, expired)) is True
    assert runtime.usage_ledger.settle(_late(row, expired)) is False
    assert only_row(runtime).state is UsageState.SETTLED
    assert tokens_counted(runtime, row) == 29


def test_a_heartbeating_task_turn_keeps_its_reservation_open():
    runtime = runtime_with_model()
    turn = killed_task_turn(runtime)
    beat = turn.updated_at + timedelta(seconds=400)
    runtime.task_turns.put(turn.model_copy(update={"updated_at": beat}))
    assert reconcile_at(runtime, beat + timedelta(seconds=30)) == 0
    assert only_row(runtime).state is UsageState.RESERVED
    lapsed = beat + timedelta(seconds=STALE_TASK_TURN_AFTER_SECONDS + 5)
    assert reconcile_at(runtime, lapsed) == 1
    assert only_row(runtime).state is UsageState.UNKNOWN


def test_a_requeued_task_turn_releases_the_dead_attempts_reservation():
    runtime = runtime_with_model()
    turn = killed_task_turn(runtime)
    runtime.task_turns.put(
        turn.model_copy(
            update={
                "status": TaskTurnStatus.QUEUED,
                "worker_id": None,
                "started_at": None,
            }
        )
    )
    assert reconcile_at(runtime, turn.updated_at + timedelta(seconds=30)) == 1
    assert only_row(runtime).state is UsageState.UNKNOWN
