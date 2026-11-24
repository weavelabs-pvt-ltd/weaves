"""Lease heartbeats run as the claiming tenant (found by the slice 5.6 load runs).

A heartbeat thread started without the claiming thread's context has no tenant
identity. Under PostgreSQL row-level security its update then matches no row, the
lease looks lost and the run is cancelled, so every task turn longer than the
heartbeat interval (15 s) failed. These tests pin the identity seen from the
renewal thread for both durable queues.
"""

import threading
import time

from contracts.v1 import ExecutionJobStatus, TaskTurnStatus
from modules.executions.runtime_completion import ExecutionCompletionMixin
from shared.request_context import current_request_identity
from tests.support.runtime import LocalPlatformRuntime


def test_task_turn_heartbeat_thread_has_the_claiming_tenant_identity(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.task_service.create_task(
        agent_id="local-assistant",
        title="Heartbeat identity",
        initial_message="Keep the lease under the tenant",
    )
    runtime.task_worker_service.heartbeat_interval_seconds = 0.005
    original_run_agent = runtime.agent_execution_service.run_agent
    seen = []

    def slow_run_agent(*args, **kwargs):
        time.sleep(0.05)
        return original_run_agent(*args, **kwargs)

    original = runtime.task_turns.put_if_worker_owned

    def observed(turn, *, worker_id, attempts):
        if threading.current_thread() is not threading.main_thread():
            seen.append(current_request_identity())
        return original(turn, worker_id=worker_id, attempts=attempts)

    monkeypatch.setattr(runtime.agent_execution_service, "run_agent", slow_run_agent)
    monkeypatch.setattr(runtime.task_turns, "put_if_worker_owned", observed)

    done = runtime.task_worker_service.process_next_turn("worker-identity")

    assert done is not None and done.status is TaskTurnStatus.SUCCEEDED
    assert seen, "the heartbeat never ran"
    assert all(identity is not None for identity in seen)
    assert {i.org_id for i in seen} == {done.org_id}


def test_execution_job_lease_thread_has_the_claiming_tenant_identity(monkeypatch):
    runtime = LocalPlatformRuntime()
    runtime.enqueue_agent_run("Keep the job lease under the tenant")
    seen = []
    original_get = runtime.execution_jobs.get

    def observed_get(job_id):
        if threading.current_thread() is not threading.main_thread():
            seen.append(current_request_identity())
        return original_get(job_id)

    original_execute = ExecutionCompletionMixin._execute_claimed_job

    def slow_execute(self, *args, **kwargs):
        time.sleep(1.3)  # the renewal thread polls once a second
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(runtime.execution_jobs, "get", observed_get)
    monkeypatch.setattr(ExecutionCompletionMixin, "_execute_claimed_job", slow_execute)

    done = runtime.process_next_execution_job("worker-identity")

    assert done is not None and done.status is ExecutionJobStatus.SUCCEEDED
    assert seen, "the lease renewal never polled"
    assert all(identity is not None for identity in seen)
    assert {i.org_id for i in seen} == {done.org_id}
