import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from application import IdempotencyConflict
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    RunStatus,
    TaskTurnStatus,
)
from tests.support.runtime import LocalPlatformRuntime


def test_bootstrap_recovers_stale_running_records_after_interruption():
    runtime = LocalPlatformRuntime()
    result = runtime.run_agent("Summarize security basics")
    stale_at = datetime.now(UTC) - timedelta(hours=5)
    runtime.runs.put(
        type(result.run).model_validate(
            result.run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "error": None,
                    "finished_at": None,
                    "updated_at": stale_at,
                }
            ).model_dump()
        )
    )
    runtime.agent_runs.put(
        type(result.agent_run).model_validate(
            result.agent_run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "error": None,
                    "finished_at": None,
                    "updated_at": stale_at,
                }
            ).model_dump()
        )
    )
    runtime.runs.create(
        type(result.run)(
            run_id="orphaned-running-record",
            org_id=result.run.org_id,
            workspace_id=result.run.workspace_id,
            workflow_id=result.run.workflow_id,
            workflow_version_id=result.run.workflow_version_id,
            requested_by_principal_id=result.run.requested_by_principal_id,
            trigger_type="local.runtime",
            status=RunStatus.RUNNING,
            started_at=stale_at,
            created_at=stale_at,
            updated_at=stale_at,
        )
    )
    runtime._bootstrapped = False

    runtime.bootstrap()

    failed_run = runtime.runs.get(result.run.run_id)
    failed_agent_run = runtime.agent_runs.get(result.agent_run.agent_run_id)
    assert failed_run.status is RunStatus.FAILED
    assert failed_run.error is not None
    assert failed_run.error.code == "runtime.interrupted"
    assert failed_agent_run.status is RunStatus.FAILED
    orphan = runtime.runs.get("orphaned-running-record")
    assert orphan.status is RunStatus.FAILED
    assert any(
        event.action == "agent.run.recovered" and event.target_id == result.run.run_id
        for event in runtime.audit_events.list()
    )


def test_stale_run_recovery_preserves_recent_active_runs():
    runtime = LocalPlatformRuntime()
    result = runtime.run_agent("Summarize security basics")
    current = datetime.now(UTC)
    running = type(result.run).model_validate(
        result.run.model_copy(
            update={
                "status": RunStatus.RUNNING,
                "error": None,
                "finished_at": None,
                "updated_at": current,
            }
        ).model_dump()
    )
    runtime.runs.put(running)

    recovered = runtime.recover_stale_runs(now=current, stale_after_seconds=120)

    assert recovered == ()
    assert runtime.runs.get(result.run.run_id).status is RunStatus.RUNNING


def test_concurrent_stale_recovery_claims_and_audits_a_run_once():
    runtime = LocalPlatformRuntime()
    result = runtime.run_agent("Summarize security basics")
    stale_at = datetime.now(UTC) - timedelta(hours=5)
    runtime.runs.put(
        type(result.run).model_validate(
            result.run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "error": None,
                    "finished_at": None,
                    "updated_at": stale_at,
                }
            ).model_dump()
        )
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = tuple(executor.map(lambda _: runtime.recover_stale_runs(), range(8)))

    assert (
        sum(run_id == result.run.run_id for recovery in results for run_id in recovery)
        == 1
    )
    recovery_events = [
        event
        for event in runtime.audit_events.list()
        if event.action == "agent.run.recovered"
        and event.target_id == result.run.run_id
    ]
    assert len(recovery_events) == 1
    runtime.audit_events.delete(recovery_events[0].audit_event_id)

    runtime.recover_stale_runs()

    assert (
        sum(
            event.action == "agent.run.recovered"
            and event.target_id == result.run.run_id
            for event in runtime.audit_events.list()
        )
        == 1
    )


def test_execution_job_queue_is_idempotent_and_worker_completes_a_run():
    runtime = LocalPlatformRuntime()
    job, replayed = runtime.enqueue_agent_run(
        "Summarize security basics", idempotency_key="queued-request-1"
    )
    assert replayed is False
    assert job.status is ExecutionJobStatus.QUEUED

    same_job, replayed = runtime.enqueue_agent_run(
        "Summarize security basics", idempotency_key="queued-request-1"
    )
    assert replayed is True
    assert same_job.job_id == job.job_id
    with pytest.raises(IdempotencyConflict):
        runtime.enqueue_agent_run(
            "Summarize another topic", idempotency_key="queued-request-1"
        )

    completed = runtime.process_next_execution_job("worker-test")
    assert completed is not None
    assert completed.job_id == job.job_id
    assert completed.status is ExecutionJobStatus.SUCCEEDED
    assert completed.attempts == 1
    assert completed.worker_id == "worker-test"
    assert completed.run_id is not None
    assert runtime.runs.get(completed.run_id).status is RunStatus.SUCCEEDED
    assert runtime.process_next_execution_job("worker-test") is None


def test_execution_job_worker_renews_lease_during_long_run(monkeypatch):
    runtime = LocalPlatformRuntime()
    task = runtime.task_service.create_task(
        agent_id="local-assistant",
        title="Heartbeat task",
        initial_message="Keep the task turn lease alive",
    )
    runtime.task_worker_service.heartbeat_interval_seconds = 0.005
    original_run_agent = runtime.agent_execution_service.run_agent
    heartbeat_calls = []

    def slow_run_agent(*args, **kwargs):
        time.sleep(0.04)
        return original_run_agent(*args, **kwargs)

    original_heartbeat = runtime.task_turns.put_if_worker_owned

    def observed_heartbeat(turn, *, worker_id, attempts):
        result = original_heartbeat(turn, worker_id=worker_id, attempts=attempts)
        heartbeat_calls.append(result)
        return result

    monkeypatch.setattr(runtime.agent_execution_service, "run_agent", slow_run_agent)
    monkeypatch.setattr(runtime.task_turns, "put_if_worker_owned", observed_heartbeat)

    completed = runtime.task_worker_service.process_next_turn("worker-with-heartbeat")

    assert completed is not None
    assert completed.task_id == task.task_id
    assert completed.status is TaskTurnStatus.SUCCEEDED
    assert heartbeat_calls
    assert any(heartbeat_calls)


def test_queued_execution_job_can_be_cancelled_idempotently():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Cancel this queued request")

    cancelled = runtime.cancel_execution_job(job.job_id)

    assert cancelled.status is ExecutionJobStatus.CANCELLED
    assert cancelled.attempts == 0
    assert cancelled.finished_at is not None
    assert runtime.cancel_execution_job(job.job_id) == cancelled
    assert runtime.process_next_execution_job("worker-test") is None
    events = [
        event
        for event in runtime.audit_events.list()
        if event.action == "agent.execution_job.cancelled"
        and event.target_id == job.job_id
    ]
    assert len(events) == 1


def test_running_execution_job_records_cancel_request():
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Already claimed request")
    claimed = runtime.execution_jobs.claim_next_queued(
        "worker-test", job.updated_at, job.org_id, job.workspace_id
    )
    assert claimed is not None

    requested = runtime.cancel_execution_job(job.job_id)

    assert requested.status is ExecutionJobStatus.CANCEL_REQUESTED
    assert requested.worker_id == claimed.worker_id
    assert requested.attempts == claimed.attempts
    assert runtime.heartbeat_execution_job(
        job.job_id, worker_id=claimed.worker_id or "", attempts=claimed.attempts
    )


def test_worker_finishes_running_job_as_cancelled_when_requested(monkeypatch):
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Cancel this active request")
    runtime.EXECUTION_JOB_HEARTBEAT_SECONDS = 0.005
    original_run_agent = runtime.run_agent

    def slow_run_agent(*args, **kwargs):
        time.sleep(0.05)
        return original_run_agent(*args, **kwargs)

    monkeypatch.setattr(runtime, "run_agent", slow_run_agent)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.process_next_execution_job, "cancel-worker")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = runtime.execution_jobs.get(job.job_id)
            if current.status is ExecutionJobStatus.RUNNING:
                break
            time.sleep(0.002)
        else:
            pytest.fail("worker did not claim the queued job")

        requested = runtime.cancel_execution_job(job.job_id)
        assert requested.status is ExecutionJobStatus.CANCEL_REQUESTED
        completed = future.result(timeout=3)

    assert completed is not None
    assert completed.status is ExecutionJobStatus.CANCELLED
    assert completed.run_id is not None
    assert runtime.runs.get(completed.run_id).status is RunStatus.CANCELLED
    cancelled_agent_runs = [
        item for item in runtime.agent_runs.list() if item.run_id == completed.run_id
    ]
    assert len(cancelled_agent_runs) == 1
    assert cancelled_agent_runs[0].status is RunStatus.CANCELLED


def test_worker_preserves_success_if_cancel_races_after_run_completion(monkeypatch):
    runtime = LocalPlatformRuntime()
    _job, _ = runtime.enqueue_agent_run("Cancel at the completion boundary")
    repository = runtime.execution_jobs
    original_put_if_owned = repository.put_if_execution_job_owned
    injected = False

    def inject_cancel_request(
        record, *, worker_id, attempts, expected_status="running"
    ):
        nonlocal injected
        if (
            not injected
            and record.status is ExecutionJobStatus.SUCCEEDED
            and expected_status == ExecutionJobStatus.RUNNING.value
        ):
            injected = True
            current = repository.get(record.job_id)
            request = ExecutionJob.model_validate(
                current.model_copy(
                    update={
                        "status": ExecutionJobStatus.CANCEL_REQUESTED,
                        "updated_at": current.updated_at + timedelta(microseconds=1),
                    }
                ).model_dump()
            )
            assert original_put_if_owned(
                request,
                worker_id=worker_id,
                attempts=attempts,
                expected_status=expected_status,
            )
            return False
        return original_put_if_owned(
            record,
            worker_id=worker_id,
            attempts=attempts,
            expected_status=expected_status,
        )

    monkeypatch.setattr(repository, "put_if_execution_job_owned", inject_cancel_request)

    completed = runtime.process_next_execution_job("racing-cancel-worker")

    assert injected
    assert completed is not None
    assert completed.status is ExecutionJobStatus.SUCCEEDED
    assert completed.run_id is not None
    assert runtime.runs.get(completed.run_id).status is RunStatus.SUCCEEDED


def test_agent_job_cancellation_stops_before_model_after_context_call(monkeypatch):
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Cancel after collecting context")
    original_invoke = runtime.plugin_gateway.invoke
    original_complete = runtime.model_gateway.complete
    model_calls = []

    def cancel_after_context(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        runtime.cancel_execution_job(job.job_id)
        return result

    def observe_model_call(*args, **kwargs):
        model_calls.append(True)
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(runtime.plugin_gateway, "invoke", cancel_after_context)
    monkeypatch.setattr(runtime.model_gateway, "complete", observe_model_call)

    completed = runtime.process_next_execution_job("agent-cancel-worker")

    assert completed is not None
    assert completed.status is ExecutionJobStatus.CANCELLED
    assert completed.run_id is not None
    assert runtime.runs.get(completed.run_id).status is RunStatus.CANCELLED
    assert model_calls == []
    assert any(
        event.action == "agent.run.cancelled" and event.target_id == completed.run_id
        for event in runtime.audit_events.list()
    )


def test_agent_job_finishes_successfully_if_cancel_arrives_during_final_model_call(
    monkeypatch,
):
    runtime = LocalPlatformRuntime()
    job, _ = runtime.enqueue_agent_run("Finish the active model call")
    original_complete = runtime.model_gateway.complete

    def cancel_during_model(*args, **kwargs):
        runtime.cancel_execution_job(job.job_id)
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(runtime.model_gateway, "complete", cancel_during_model)

    completed = runtime.process_next_execution_job("agent-late-cancel-worker")

    assert completed is not None
    assert completed.status is ExecutionJobStatus.SUCCEEDED
    assert completed.run_id is not None
    assert runtime.runs.get(completed.run_id).status is RunStatus.SUCCEEDED
