"""Phase 5.4 on real PostgreSQL: recovery batches, overview parity, email probe.

Set WEAVES_TEST_DATABASE_URL to a disposable database. These run the real
services over PostgreSQL; the index and scale checks are in
``test_bounded_queries_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.v1 import (
    ExecutionJobStatus,
    Permission,
    RunStatus,
    TaskAudience,
)
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.overview_data import (
    MemoryOverviewData,
    PostgresOverviewData,
)
from tests.support.runtime import LocalPlatformRuntime


def _stale(runtime, hours: int = 9):
    return datetime.now(UTC) - timedelta(hours=hours)


def test_recovery_walks_batches_and_matches_the_old_full_scan(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        runtime = LocalPlatformRuntime(postgres_store=store)
        runtime.bootstrap()
        result = runtime.run_agent("Summarize security basics")
        old = _stale(runtime)
        template = result.run
        stale_ids = []
        for index in range(7):
            run = type(template).model_validate(
                template.model_copy(
                    update={
                        "run_id": f"stale-run-{index}",
                        "idempotency_key": None,
                        "status": RunStatus.RUNNING,
                        "error": None,
                        "finished_at": None,
                        "updated_at": old,
                    }
                ).model_dump()
            )
            runtime.runs.create(run)
            stale_ids.append(run.run_id)
        fresh = type(template).model_validate(
            template.model_copy(
                update={
                    "run_id": "fresh-run",
                    "idempotency_key": None,
                    "status": RunStatus.RUNNING,
                    "error": None,
                    "finished_at": None,
                    "updated_at": datetime.now(UTC),
                }
            ).model_dump()
        )
        runtime.runs.create(fresh)
        with store.pool.connection() as connection:
            connection.execute(
                """UPDATE product_records SET updated_at = now() - interval '9 hours'
                   WHERE collection = 'workflow_runs'
                     AND record_id LIKE 'stale-run-%'"""
            )
        scan = {
            run.run_id
            for run in runtime.runs.list()
            if run.status is RunStatus.RUNNING
            and run.updated_at <= datetime.now(UTC) - timedelta(seconds=7_200)
        }
        runtime.recovery_service._batch_size = 3  # forces three bounded passes
        recovered = set(runtime.recover_stale_runs(stale_after_seconds=7_200))
        assert recovered == scan == set(stale_ids)
        assert runtime.runs.get("fresh-run").status is RunStatus.RUNNING
        assert runtime.recover_stale_runs(stale_after_seconds=7_200) == ()
        events = [
            event
            for event in runtime.audit_events.list()
            if event.action == "agent.run.recovered"
            and event.target_id in set(stale_ids)
        ]
        assert len(events) == 7
    finally:
        store.close()


def test_stale_jobs_recover_in_batches(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        runtime = LocalPlatformRuntime(postgres_store=store)
        runtime.bootstrap()
        job, _ = runtime.enqueue_agent_run("Summarize security basics")
        claimed = runtime.execution_jobs.claim_next_queued(
            "w1", datetime.now(UTC), None, None
        )
        assert claimed is not None and claimed.job_id == job.job_id
        with store.pool.connection() as connection:
            connection.execute(
                """UPDATE product_records SET updated_at = now() - interval '9 hours'
                   WHERE collection = 'execution_jobs'"""
            )
        runtime.recovery_service._batch_size = 1
        recovered = runtime.recover_stale_execution_jobs(stale_after_seconds=60)
        assert recovered == (job.job_id,)
        final = runtime.execution_jobs.get(job.job_id)
        assert final.status in {
            ExecutionJobStatus.FAILED,
            ExecutionJobStatus.SUCCEEDED,
        }
    finally:
        store.close()


def test_overview_matches_the_in_memory_oracle_on_real_records(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        runtime = LocalPlatformRuntime(postgres_store=store)
        runtime.bootstrap()
        for index in range(4):
            runtime.task_service.create_task(
                agent_id="local-assistant",
                title=f"Task {index}",
                initial_message="Summarize security basics",
                audience=TaskAudience.PRIVATE if index == 3 else TaskAudience.WORKSPACE,
            )
        runtime.task_worker_service.process_next_turn("pg-overview")
        task = runtime.task_service.list_tasks()[0]
        args = {
            "now": datetime.now(UTC),
            "tz_offset_minutes": 330,
        }
        real = PostgresOverviewData(store).snapshot(
            task.org_id, task.workspace_id, "local-developer", **args
        )
        oracle = MemoryOverviewData(runtime._repository_set).snapshot(
            task.org_id, task.workspace_id, "local-developer", **args
        )
        assert real == oracle
        assert real["tasks"]["total"] == 4
        assert (
            runtime.overview_service.overview(tz_offset_minutes=330)["tasks"]["total"]
            == 4
        )
    finally:
        store.close()


def test_email_availability_is_a_single_indexed_probe(isolated_postgres_url):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        runtime = LocalPlatformRuntime(postgres_store=store)
        runtime.bootstrap()
        service = runtime.user_administration_service
        user, _principal, _role = service.create_workspace_user(
            email="Dana@Example.com",
            display_name="Dana",
            permissions=(Permission.AGENTS_VIEW,),
            role_id=None,
        )
        with pytest.raises(ValueError, match="already belongs"):
            service.create_workspace_user(
                email="dana@example.com",
                display_name="Dana again",
                permissions=(Permission.AGENTS_VIEW,),
                role_id=None,
            )
        assert user.email == "dana@example.com"
    finally:
        store.close()
