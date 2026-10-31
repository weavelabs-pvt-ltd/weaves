from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    EvaluationExecution,
    EvaluationExecutionStatus,
    ExecutionJob,
    ExecutionJobStatus,
)
from modules.evaluations.scoring import (
    EvaluationCase,
    EvaluationSuite,
    score_evaluation_case,
)
from tests.support.runtime import LocalPlatformRuntime


def test_queued_evaluation_runs_cases_and_persists_a_verified_report():
    suite = EvaluationSuite(
        suite_id="queued-smoke",
        agent_id="local-assistant",
        cases=(
            EvaluationCase(
                case_id="marker-a",
                task="Return the LOCAL-SMOKE-OK marker.",
                must_contain=("LOCAL-SMOKE-OK",),
            ),
            EvaluationCase(
                case_id="marker-b",
                task="Return the LOCAL-SMOKE-OK marker again.",
                must_contain=("LOCAL-SMOKE-OK",),
            ),
        ),
    )
    runtime = LocalPlatformRuntime()
    runtime.evaluation_service.create_evaluation_suite(suite)

    queued, replayed = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="queued-evaluation-001"
    )
    replay, was_replayed = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="queued-evaluation-001"
    )
    assert not replayed
    assert was_replayed
    assert replay.evaluation_id == queued.evaluation_id
    assert queued.status is EvaluationExecutionStatus.QUEUED

    job = runtime.process_next_execution_job("evaluation-worker-1")
    assert job is not None
    assert job.evaluation_id == queued.evaluation_id
    assert job.run_id is None
    assert job.status.value == "succeeded"

    completed = runtime.evaluation_service.get_evaluation_execution(
        queued.evaluation_id
    )
    report = runtime.evaluation_service.get_evaluation_report(queued.evaluation_id)
    assert completed.status is EvaluationExecutionStatus.SUCCEEDED
    assert [result.case_id for result in completed.results] == [
        "marker-a",
        "marker-b",
    ]
    assert all(result.passed for result in completed.results)
    assert report.report.pass_rate == 1.0


def test_queued_evaluation_can_be_cancelled_before_a_worker_claims_it():
    runtime = LocalPlatformRuntime()
    suite = EvaluationSuite(
        suite_id="queued-cancel",
        agent_id="local-assistant",
        cases=(EvaluationCase(case_id="case-a", task="Check the status."),),
    )
    runtime.evaluation_service.create_evaluation_suite(suite)
    queued, _ = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="queued-evaluation-cancel"
    )

    cancelled = runtime.evaluation_service.cancel_evaluation_run(queued.evaluation_id)

    assert cancelled.status is EvaluationExecutionStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.results == ()
    assert runtime.process_next_execution_job("evaluation-worker-2") is None


def test_running_evaluation_cancels_between_cases_and_keeps_completed_progress(
    monkeypatch,
):
    runtime = LocalPlatformRuntime()
    runtime.EXECUTION_JOB_HEARTBEAT_SECONDS = 0.01
    suite = EvaluationSuite(
        suite_id="running-evaluation-cancel",
        agent_id="local-assistant",
        cases=(
            EvaluationCase(case_id="case-a", task="Return LOCAL-SMOKE-OK."),
            EvaluationCase(case_id="case-b", task="Return LOCAL-SMOKE-OK again."),
        ),
    )
    runtime.evaluation_service.create_evaluation_suite(suite)
    queued, _ = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="running-evaluation-cancel-001"
    )
    first_case_completed = Event()
    release_first_case = Event()
    original_run_agent = runtime.run_agent
    run_count = 0

    def pause_after_first_case(*args, **kwargs):
        nonlocal run_count
        result = original_run_agent(*args, **kwargs)
        run_count += 1
        if run_count == 1:
            first_case_completed.set()
            assert release_first_case.wait(timeout=3)
        return result

    monkeypatch.setattr(runtime, "run_agent", pause_after_first_case)
    # Keep heartbeat signaling out of the test so cancellation must be observed
    # directly from the durable job state at the next case boundary.
    monkeypatch.setattr(runtime, "_renew_execution_job_lease", lambda *args: None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            runtime.process_next_execution_job, "evaluation-cancel-worker"
        )
        assert first_case_completed.wait(timeout=3)
        requested = runtime.evaluation_service.cancel_evaluation_run(
            queued.evaluation_id
        )
        assert requested.status is EvaluationExecutionStatus.CANCEL_REQUESTED
        release_first_case.set()
        completed_job = future.result(timeout=5)

    completed = runtime.evaluation_service.get_evaluation_execution(
        queued.evaluation_id
    )
    assert completed_job is not None
    assert completed_job.status.value == "cancelled"
    assert completed.status is EvaluationExecutionStatus.CANCELLED
    assert [result.case_id for result in completed.results] == ["case-a"]
    assert run_count == 1
    with pytest.raises(KeyError, match="not found"):
        runtime.evaluation_service.get_evaluation_report(queued.evaluation_id)


def test_interrupted_partial_evaluation_recovers_as_non_retryable_failure():
    runtime = LocalPlatformRuntime()
    suite = EvaluationSuite(
        suite_id="partial-recovery-smoke",
        agent_id="local-assistant",
        cases=(
            EvaluationCase(case_id="case-a", task="Check the first status."),
            EvaluationCase(case_id="case-b", task="Check the second status."),
        ),
    )
    runtime.evaluation_service.create_evaluation_suite(suite)
    queued, _ = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="partial-recovery-001"
    )
    now = datetime.now(UTC)
    stale_at = now - timedelta(seconds=5)
    claimed = runtime.execution_jobs.claim_next_queued(
        "interrupted-worker", stale_at, queued.org_id, queued.workspace_id
    )
    assert claimed is not None
    runtime.execution_jobs.put(
        ExecutionJob.model_validate(
            claimed.model_copy(update={"updated_at": stale_at}).model_dump()
        )
    )
    evaluation = runtime.evaluation_executions.get(queued.evaluation_id)
    partial_result = score_evaluation_case(
        suite.cases[0],
        status=None,
        run_id=None,
        request_error="Worker stopped after the first case.",
    )
    runtime.evaluation_executions.put(
        EvaluationExecution.model_validate(
            evaluation.model_copy(
                update={
                    "status": EvaluationExecutionStatus.RUNNING,
                    "started_at": stale_at,
                    "updated_at": stale_at,
                    "results": (partial_result,),
                }
            ).model_dump()
        )
    )

    recovered = runtime.recover_stale_execution_jobs(stale_after_seconds=1, now=now)

    assert recovered == (queued.execution_job_id,)
    job = runtime.execution_jobs.get(queued.execution_job_id)
    failed = runtime.evaluation_service.get_evaluation_execution(queued.evaluation_id)
    assert job.status is ExecutionJobStatus.FAILED
    assert job.error is not None and not job.error.retryable
    assert failed.status is EvaluationExecutionStatus.FAILED
    assert failed.error is not None and failed.error.code == "runtime.interrupted"
    assert [result.case_id for result in failed.results] == ["case-a"]
    with pytest.raises(ValueError, match="evaluation jobs cannot be retried"):
        runtime.retry_execution_job(
            queued.execution_job_id, idempotency_key="partial-recovery-retry"
        )


def test_interrupted_evaluation_with_all_saved_cases_recovers_report():
    runtime = LocalPlatformRuntime()
    suite = EvaluationSuite(
        suite_id="complete-recovery-smoke",
        agent_id="local-assistant",
        cases=(
            EvaluationCase(case_id="case-a", task="Check the first status."),
            EvaluationCase(case_id="case-b", task="Check the second status."),
        ),
    )
    runtime.evaluation_service.create_evaluation_suite(suite)
    queued, _ = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="complete-recovery-001"
    )
    now = datetime.now(UTC)
    stale_at = now - timedelta(seconds=5)
    claimed = runtime.execution_jobs.claim_next_queued(
        "interrupted-worker", stale_at, queued.org_id, queued.workspace_id
    )
    assert claimed is not None
    runtime.execution_jobs.put(
        ExecutionJob.model_validate(
            claimed.model_copy(update={"updated_at": stale_at}).model_dump()
        )
    )
    evaluation = runtime.evaluation_executions.get(queued.evaluation_id)
    results = tuple(
        score_evaluation_case(
            case,
            status=None,
            run_id=None,
            request_error="Provider was unavailable for this completed case.",
        )
        for case in suite.cases
    )
    runtime.evaluation_executions.put(
        EvaluationExecution.model_validate(
            evaluation.model_copy(
                update={
                    "status": EvaluationExecutionStatus.RUNNING,
                    "started_at": stale_at,
                    "updated_at": stale_at,
                    "results": results,
                }
            ).model_dump()
        )
    )

    recovered = runtime.recover_stale_execution_jobs(stale_after_seconds=1, now=now)

    assert recovered == (queued.execution_job_id,)
    job = runtime.execution_jobs.get(queued.execution_job_id)
    completed = runtime.evaluation_service.get_evaluation_execution(
        queued.evaluation_id
    )
    report = runtime.evaluation_service.get_evaluation_report(queued.evaluation_id)
    assert job.status is ExecutionJobStatus.SUCCEEDED
    assert completed.status is EvaluationExecutionStatus.SUCCEEDED
    assert report.report.total_cases == 2
    assert report.report.passed_cases == 0
    assert report.report.pass_rate == 0.0


def test_cancel_requested_evaluation_recovers_cancelled_after_worker_loss():
    runtime = LocalPlatformRuntime()
    suite = EvaluationSuite(
        suite_id="cancel-recovery-smoke",
        agent_id="local-assistant",
        cases=(EvaluationCase(case_id="case-a", task="Check the service."),),
    )
    runtime.evaluation_service.create_evaluation_suite(suite)
    queued, _ = runtime.evaluation_service.enqueue_evaluation_run(
        suite.suite_id, idempotency_key="cancel-recovery-001"
    )
    now = datetime.now(UTC)
    stale_at = now - timedelta(seconds=5)
    claimed = runtime.execution_jobs.claim_next_queued(
        "interrupted-worker", stale_at, queued.org_id, queued.workspace_id
    )
    assert claimed is not None
    running = runtime.evaluation_executions.get(queued.evaluation_id)
    runtime.evaluation_executions.put(
        EvaluationExecution.model_validate(
            running.model_copy(
                update={
                    "status": EvaluationExecutionStatus.RUNNING,
                    "started_at": stale_at,
                    "updated_at": stale_at,
                }
            ).model_dump()
        )
    )
    runtime.evaluation_service.cancel_evaluation_run(queued.evaluation_id)
    requested_job = runtime.execution_jobs.get(queued.execution_job_id)
    requested_evaluation = runtime.evaluation_executions.get(queued.evaluation_id)
    runtime.execution_jobs.put(
        ExecutionJob.model_validate(
            requested_job.model_copy(update={"updated_at": stale_at}).model_dump()
        )
    )
    runtime.evaluation_executions.put(
        EvaluationExecution.model_validate(
            requested_evaluation.model_copy(
                update={"updated_at": stale_at}
            ).model_dump()
        )
    )

    recovered = runtime.recover_stale_execution_jobs(stale_after_seconds=1, now=now)

    assert recovered == (queued.execution_job_id,)
    assert (
        runtime.execution_jobs.get(queued.execution_job_id).status
        is ExecutionJobStatus.CANCELLED
    )
    cancelled = runtime.evaluation_service.get_evaluation_execution(
        queued.evaluation_id
    )
    assert cancelled.status is EvaluationExecutionStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert cancelled.results == ()


def test_evaluation_run_api_enqueues_replays_and_cancels(monkeypatch):
    monkeypatch.setenv("WEAVES_API_TOKEN", "evaluation-api-test-token")
    monkeypatch.delenv("WEAVES_API_TOKENS", raising=False)
    runtime = LocalPlatformRuntime()
    suite = EvaluationSuite(
        suite_id="api-queued-smoke",
        agent_id="local-assistant",
        cases=(EvaluationCase(case_id="case-a", task="Check the status."),),
    )
    runtime.evaluation_service.create_evaluation_suite(suite)
    client = TestClient(
        create_app(runtime),
        headers={"Authorization": "Bearer evaluation-api-test-token"},
    )

    with client:
        unavailable = client.post(
            f"/api/v0/evaluation-suites/{suite.suite_id}/runs",
            headers={"Idempotency-Key": "api-queued-eval-001"},
        )
        assert unavailable.status_code == 503

        # The HTTP route requires durable storage. Mark the test runtime as
        # PostgreSQL-backed to exercise its request/response contract without
        # requiring a live database in this unit test.
        runtime.storage_mode = "postgres"
        submitted = client.post(
            f"/api/v0/evaluation-suites/{suite.suite_id}/runs",
            headers={"Idempotency-Key": "api-queued-eval-001"},
        )
        assert submitted.status_code == 202, submitted.text
        evaluation_id = submitted.json()["evaluation"]["evaluation_id"]
        assert submitted.json()["evaluation"]["status"] == "queued"
        assert submitted.json()["replayed"] is False

        replay = client.post(
            f"/api/v0/evaluation-suites/{suite.suite_id}/runs",
            headers={"Idempotency-Key": "api-queued-eval-001"},
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["evaluation"]["evaluation_id"] == evaluation_id
        assert replay.json()["replayed"] is True

        listed = client.get("/api/v0/evaluation-runs")
        assert listed.status_code == 200, listed.text
        assert [item["evaluation_id"] for item in listed.json()] == [evaluation_id]

        fetched = client.get(f"/api/v0/evaluation-runs/{evaluation_id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["status"] == "queued"

        cancelled = client.post(f"/api/v0/evaluation-runs/{evaluation_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
