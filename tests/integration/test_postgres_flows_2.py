"""Real PostgreSQL checks for flows whose guarantees cross API processes.

Set WEAVES_TEST_DATABASE_URL to a disposable database URL. Each test creates
and drops its own schema, so it does not touch the database's public schema.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest

from contracts.v1 import (
    EvaluationCase,
    EvaluationExecutionStatus,
    EvaluationReport,
    EvaluationSuite,
    ExecutionJob,
    ExecutionJobStatus,
    WorkflowSchedule,
    WorkflowScheduleStatus,
    WorkflowTriggerStatus,
)
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.repositories import (
    IdempotencyKeyAlreadyExists,
    PostgresRepository,
)
from modules.evaluations.scoring import score_evaluation_case
from tests.support.runtime import LocalPlatformRuntime


def test_postgres_execution_job_is_claimed_once_across_worker_pools(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    second_store = ProductPostgresStore(isolated_postgres_url)
    try:
        first = PostgresRepository(
            first_store.pool, "execution_jobs", "job_id", ExecutionJob
        )
        second = PostgresRepository(
            second_store.pool, "execution_jobs", "job_id", ExecutionJob
        )
        now = datetime.now(UTC)
        first.create(
            ExecutionJob(
                job_id="job-claim-race",
                org_id="org-claim-race",
                workspace_id="workspace-claim-race",
                requested_by_principal_id="principal-claim-race",
                task="Run the integration test task",
                idempotency_key="request-claim-race",
                status=ExecutionJobStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
        )

        def claim(
            repository: PostgresRepository[ExecutionJob], worker_id: str
        ) -> ExecutionJob | None:
            return repository.claim_next_queued(
                worker_id,
                datetime.now(UTC),
                "org-claim-race",
                "workspace-claim-race",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = tuple(
                executor.map(claim, (first, second), ("worker-a", "worker-b"))
            )

        winners = tuple(job for job in claims if job is not None)
        assert len(winners) == 1
        assert winners[0].status is ExecutionJobStatus.RUNNING
        assert winners[0].attempts == 1
        assert winners[0].worker_id in {"worker-a", "worker-b"}
        assert first.get("job-claim-race").status is ExecutionJobStatus.RUNNING
    finally:
        second_store.close()
        first_store.close()


def test_postgres_webhook_delivery_idempotency_is_shared_across_pools(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    second_store = ProductPostgresStore(isolated_postgres_url)
    try:
        first = PostgresRepository(
            first_store.pool, "execution_jobs", "job_id", ExecutionJob
        )
        second = PostgresRepository(
            second_store.pool, "execution_jobs", "job_id", ExecutionJob
        )
        now = datetime.now(UTC)
        event_job = ExecutionJob(
            job_id="job-webhook-event-1",
            org_id="org-webhook-event",
            workspace_id="workspace-webhook-event",
            requested_by_principal_id="principal-webhook-event",
            task="Process the untrusted webhook event.",
            workflow_id="workflow-webhook-event",
            trigger_id="trigger-webhook-event",
            trigger_event_id="provider-delivery-123",
            idempotency_key="webhook-unique-event-digest",
            status=ExecutionJobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        first.create(event_job)
        duplicate = event_job.model_copy(update={"job_id": "job-webhook-event-2"})

        with pytest.raises(IdempotencyKeyAlreadyExists):
            second.create(duplicate)

        persisted = second.find_by_idempotency_key(
            event_job.idempotency_key,
            event_job.requested_by_principal_id,
            event_job.org_id,
            event_job.workspace_id,
        )
        assert persisted is not None
        assert persisted.job_id == event_job.job_id
        assert persisted.trigger_id == event_job.trigger_id
        assert persisted.trigger_event_id == event_job.trigger_event_id
    finally:
        second_store.close()
        first_store.close()


def test_postgres_trigger_disable_serializes_with_delivery_across_runtime_pools(
    isolated_postgres_url, monkeypatch
):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setenv(
        "WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    first = LocalPlatformRuntime()
    second = LocalPlatformRuntime()
    first.bootstrap()
    second.bootstrap()
    trigger, token = first.create_workflow_trigger(
        "local-assistant-workflow",
        name="Cross-pool event",
        task_instructions="Summarize the event.",
    )
    enqueue_entered = Event()
    allow_enqueue = Event()
    disable_started = Event()
    disable_finished = Event()
    actual_enqueue = first.enqueue_agent_run

    def paused_enqueue(*args, **kwargs):
        enqueue_entered.set()
        assert allow_enqueue.wait(timeout=5)
        return actual_enqueue(*args, **kwargs)

    first.enqueue_agent_run = paused_enqueue

    def disable_trigger():
        disable_started.set()
        updated = second.update_workflow_trigger(
            trigger.trigger_id, WorkflowTriggerStatus.DISABLED
        )
        disable_finished.set()
        return updated

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            delivery = executor.submit(
                first.enqueue_workflow_trigger_event,
                trigger.trigger_id,
                token,
                "cross-pool-delivery-1",
                {"issue": "REL-42"},
            )
            assert enqueue_entered.wait(timeout=5)
            disablement = executor.submit(disable_trigger)
            assert disable_started.wait(timeout=5)
            assert not disable_finished.wait(timeout=0.1)
            allow_enqueue.set()
            job, replayed = delivery.result(timeout=5)
            disabled = disablement.result(timeout=5)

        assert not replayed
        assert job.trigger_id == trigger.trigger_id
        assert disabled.status is WorkflowTriggerStatus.DISABLED
        with pytest.raises(KeyError, match="not found"):
            first.enqueue_workflow_trigger_event(
                trigger.trigger_id,
                token,
                "cross-pool-delivery-2",
                {"issue": "REL-43"},
            )
    finally:
        allow_enqueue.set()
        second.close()
        first.close()


def test_postgres_schedule_occurrence_is_queued_once_across_runtime_pools(
    isolated_postgres_url, monkeypatch
):
    """Two independent workers must not enqueue the same due occurrence."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setenv(
        "WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    first = LocalPlatformRuntime()
    second = LocalPlatformRuntime()
    first.bootstrap()
    second.bootstrap()
    schedule = first.create_workflow_schedule(
        "local-assistant-workflow",
        name="Cross-pool recurring summary",
        task_instructions="Summarize the open issue queue.",
        interval_seconds=60,
    )
    now = datetime.now(UTC)
    due = WorkflowSchedule.model_validate(
        schedule.model_copy(
            update={"next_run_at": now - timedelta(seconds=1)}
        ).model_dump()
    )
    first.workflow_schedules.put(due)
    start = Barrier(3)

    def dispatch(runtime: LocalPlatformRuntime):
        start.wait(timeout=5)
        return runtime.dispatch_due_workflow_schedules(now=now)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_dispatch = executor.submit(dispatch, first)
            second_dispatch = executor.submit(dispatch, second)
            start.wait(timeout=5)
            jobs = first_dispatch.result(timeout=10) + second_dispatch.result(
                timeout=10
            )

        assert len(jobs) == 1
        assert jobs[0].schedule_id == schedule.schedule_id
        assert jobs[0].schedule_occurrence_at == due.next_run_at
        persisted = second.workflow_schedules.get(schedule.schedule_id)
        assert persisted.status is WorkflowScheduleStatus.ACTIVE
        assert persisted.last_run_at == due.next_run_at
        assert persisted.next_run_at == now + timedelta(
            seconds=schedule.interval_seconds
        )
    finally:
        second.close()
        first.close()


def test_postgres_evaluation_suite_and_report_are_shared_across_runtime_pools(
    isolated_postgres_url, monkeypatch
):
    """Saved definitions and immutable reports survive API pool boundaries."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setenv(
        "WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    first = LocalPlatformRuntime()
    second = LocalPlatformRuntime()
    first.bootstrap()
    second.bootstrap()
    definition = EvaluationSuite(
        suite_id="postgres-evaluation-smoke",
        agent_id="local-assistant",
        cases=(EvaluationCase(case_id="provider-unavailable", task="Check status."),),
    )
    try:
        stored_suite = first.evaluation_service.create_evaluation_suite(definition)
        result = score_evaluation_case(
            definition.cases[0],
            status=None,
            run_id=None,
            request_error="Run API returned HTTP 503.",
            duration_ms=12,
        )
        report = EvaluationReport(
            suite_id=definition.suite_id,
            evaluation_id="postgres-evaluation-report-1",
            passed_cases=0,
            total_cases=1,
            pass_rate=0.0,
            results=(result,),
        )
        first.evaluation_service.store_evaluation_report(definition, report)

        reloaded_suite = second.evaluation_service.get_evaluation_suite(
            definition.suite_id
        )
        reloaded_report = second.evaluation_service.get_evaluation_report(
            report.evaluation_id
        )
        assert reloaded_suite.definition == stored_suite.definition
        assert reloaded_report.report == report
        assert reloaded_report.definition == definition
        assert reloaded_report.submitted_by_principal_id == "local-developer"
    finally:
        second.close()
        first.close()


def test_postgres_queued_evaluation_runs_across_runtime_pools(
    isolated_postgres_url, monkeypatch
):
    """A separate worker pool can finish a durable evaluation submitted elsewhere."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setenv(
        "WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    api_runtime = LocalPlatformRuntime()
    worker_runtime = LocalPlatformRuntime()
    api_runtime.bootstrap()
    worker_runtime.bootstrap()
    definition = EvaluationSuite(
        suite_id="postgres-queued-evaluation",
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
    try:
        api_runtime.evaluation_service.create_evaluation_suite(definition)
        start = Barrier(3)

        def submit(runtime: LocalPlatformRuntime):
            start.wait(timeout=5)
            return runtime.evaluation_service.enqueue_evaluation_run(
                definition.suite_id,
                idempotency_key="postgres-evaluation-job-001",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_submission = executor.submit(submit, api_runtime)
            second_submission = executor.submit(submit, worker_runtime)
            start.wait(timeout=5)
            submissions = (
                first_submission.result(timeout=10),
                second_submission.result(timeout=10),
            )
        submitted = [execution for execution, _ in submissions]
        replay_states = [was_replayed for _, was_replayed in submissions]
        assert sorted(replay_states) == [False, True]
        assert submitted[0].evaluation_id == submitted[1].evaluation_id
        queued = submitted[0]
        assert (
            worker_runtime.evaluation_service.get_evaluation_execution(
                queued.evaluation_id
            ).status
            is EvaluationExecutionStatus.QUEUED
        )

        completed_job = worker_runtime.process_next_execution_job(
            "postgres-evaluation-worker"
        )

        assert completed_job is not None
        assert completed_job.evaluation_id == queued.evaluation_id
        assert completed_job.status is ExecutionJobStatus.SUCCEEDED
        _assert_completed_evaluation(api_runtime, queued.evaluation_id)
    finally:
        worker_runtime.close()
        api_runtime.close()


def _assert_completed_evaluation(
    runtime: LocalPlatformRuntime, evaluation_id: str
) -> None:
    completed = runtime.evaluation_service.get_evaluation_execution(evaluation_id)
    report = runtime.evaluation_service.get_evaluation_report(evaluation_id)
    assert completed.status is EvaluationExecutionStatus.SUCCEEDED
    assert [result.case_id for result in completed.results] == ["marker-a", "marker-b"]
    assert all(result.passed for result in completed.results)
    assert report.report.pass_rate == 1.0
