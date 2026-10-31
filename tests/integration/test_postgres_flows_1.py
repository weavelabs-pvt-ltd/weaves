"""Real PostgreSQL checks for flows whose guarantees cross API processes.

Set WEAVES_TEST_DATABASE_URL to a disposable database URL. Each test creates
and drops its own schema, so it does not touch the database's public schema.
"""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from contracts.v1 import (
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    RunStatus,
    WorkflowConditionOperator,
    WorkflowJoinPolicy,
    WorkflowParallelGroup,
    WorkflowRun,
    WorkflowStepCondition,
    WorkflowStepDependency,
)
from infrastructure.auth.throttle import (
    PostgresLoginRateLimiter,
    login_bucket_key,
)
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.repositories import (
    IdempotencyKeyAlreadyExists,
    PostgresRepository,
)
from tests.support.runtime import LocalPlatformRuntime


def isolated_postgres_url() -> Iterator[str]:
    database_url = os.environ.get("WEAVES_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set WEAVES_TEST_DATABASE_URL to run PostgreSQL integration tests")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"weaves_it_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_url = make_conninfo(
        database_url, options=f"-c search_path={schema},public"
    )
    try:
        yield isolated_url
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def test_postgres_login_throttle_is_shared_across_independent_pools(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    second_store = ProductPostgresStore(isolated_postgres_url)
    try:
        first = PostgresLoginRateLimiter(first_store.pool, limit=3, window_seconds=60)
        second = PostgresLoginRateLimiter(second_store.pool, limit=3, window_seconds=60)
        now = datetime.now(UTC)
        bucket = login_bucket_key("account", "owner@example.test")

        assert first.consume(bucket, now).allowed
        assert second.consume(bucket, now).allowed
        assert first.consume(bucket, now).allowed
        locked = second.consume(bucket, now)

        assert not locked.allowed
        assert locked.retry_after_seconds == 60
        first.clear(bucket)
        assert second.consume(bucket, now).allowed
    finally:
        second_store.close()
        first_store.close()


def test_postgres_schema_migrations_are_ordered_and_idempotent(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    try:
        with first_store.connection() as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM product_schema_migrations ORDER BY version"
                ).fetchall()
            )
        assert versions == tuple(range(1, 13))

        # A second API/worker startup must observe the applied history and leave
        # the migration ledger unchanged.
        second_store = ProductPostgresStore(isolated_postgres_url)
        try:
            with second_store.connection() as connection:
                reapplied_versions = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM product_schema_migrations ORDER BY version"
                    ).fetchall()
                )
            assert reapplied_versions == versions
        finally:
            second_store.close()
    finally:
        first_store.close()


def test_postgres_workflow_step_replacement_persists_published_versions(
    isolated_postgres_url, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.delenv("WEAVES_SECRET_ENCRYPTION_KEY", raising=False)
    runtime = LocalPlatformRuntime()
    try:
        runtime.bootstrap()
        context = _prepare_postgres_workflow_version_replacement(runtime)
    finally:
        runtime.close()

    reopened = LocalPlatformRuntime()
    try:
        reopened.bootstrap()
        persisted_version = _assert_postgres_workflow_version_persisted(
            reopened, context
        )
        _assert_pinned_workflow_job_uses_old_version(reopened, context)
        _assert_next_workflow_job_uses_published_version(
            reopened, context, persisted_version
        )
    finally:
        reopened.close()


def _prepare_postgres_workflow_version_replacement(runtime):
    """Queue an old workflow version, then publish a conditional parallel version."""
    first = runtime.agent_service.create_agent(
        name="Existing step", instructions="Review."
    )
    second = runtime.agent_service.create_agent(
        name="New step", instructions="Summarize."
    )
    third = runtime.agent_service.create_agent(
        name="Parallel step", instructions="Check risks."
    )
    fourth = runtime.agent_service.create_agent(
        name="Parallel step two", instructions="Check scope."
    )
    workflow = runtime.create_workflow(name="Versioned sequence", agent_id=first.id)
    queued, replayed = runtime.enqueue_agent_run(
        "Run the originally accepted workflow",
        workflow_id=workflow.workflow_id,
        idempotency_key="workflow-version-pin",
    )
    assert replayed is False
    assert queued.workflow_version_id == workflow.current_version_id
    condition = WorkflowStepCondition(
        source_agent_id=first.id,
        target_agent_id=second.id,
        json_pointer="/route",
        operator=WorkflowConditionOperator.EXISTS,
        expected_value=None,
    )
    updated = runtime.replace_workflow_agents(
        workflow.workflow_id,
        (first.id, second.id, third.id, fourth.id),
        (condition,),
        parallel_groups=(WorkflowParallelGroup(agent_ids=(third.id, fourth.id)),),
        step_dependencies=tuple(
            WorkflowStepDependency(
                target_agent_id=agent_id, depends_on_agent_ids=(first.id,)
            )
            for agent_id in (second.id, third.id, fourth.id)
        ),
    )
    assert updated.current_version_id != workflow.current_version_id
    version = runtime.workflow_versions.get_scoped(
        updated.current_version_id, updated.org_id, updated.workspace_id
    )
    assert version.version == 2
    assert len(version.agent_version_ids) == 4
    assert version.step_conditions == (condition,)
    assert version.parallel_groups == (
        WorkflowParallelGroup(agent_ids=(third.id, fourth.id)),
    )
    assert version.step_dependencies[0].join_policy is WorkflowJoinPolicy.ALL
    return first, third, fourth, workflow, queued, updated, condition, version


def _assert_postgres_workflow_version_persisted(runtime, context):
    """Reload the replacement workflow version from PostgreSQL."""
    _, _, _, workflow, _, updated, condition, version = context
    persisted = runtime.workflows.get_scoped(
        workflow.workflow_id, workflow.org_id, workflow.workspace_id
    )
    current = runtime.workflow_versions.get_scoped(
        persisted.current_version_id or "", persisted.org_id, persisted.workspace_id
    )
    assert current.workflow_version_id == updated.current_version_id
    assert len(current.agent_version_ids) == 4
    assert current.step_conditions == (condition,)
    assert current.parallel_groups == version.parallel_groups
    assert current.step_dependencies == version.step_dependencies
    return current


def _assert_pinned_workflow_job_uses_old_version(runtime, context):
    """Process a queued job and verify it remains pinned to its prior version."""
    _, _, _, _, queued, _, _, _ = context
    completed = runtime.process_next_execution_job("workflow-version-test-worker")
    assert completed is not None
    assert completed.status.value == "succeeded"
    run = runtime.runs.get(completed.run_id or "")
    assert run.workflow_version_id == queued.workflow_version_id


def _assert_next_workflow_job_uses_published_version(runtime, context, version):
    """Queue and execute the published parallel workflow version."""
    first, third, fourth, workflow, *_ = context
    queued, _ = runtime.enqueue_agent_run(
        "Run the new parallel workflow",
        workflow_id=workflow.workflow_id,
        idempotency_key="workflow-parallel-version",
    )
    assert queued.workflow_version_id == version.workflow_version_id
    completed = runtime.process_next_execution_job("workflow-parallel-test-worker")
    assert completed is not None
    assert completed.status.value == "succeeded"
    run = runtime.runs.get(completed.run_id or "")
    agent_runs = [
        item
        for item in runtime.agent_runs.list_scoped(run.org_id, run.workspace_id)
        if item.run_id == run.run_id
    ]
    assert {item.agent_id for item in agent_runs} == {first.id, third.id, fourth.id}


def test_postgres_approval_compare_and_set_allows_one_concurrent_decision(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    second_store = ProductPostgresStore(isolated_postgres_url)
    try:
        first = PostgresRepository(
            first_store.pool,
            "approval_requests",
            "approval_request_id",
            ApprovalRequest,
        )
        second = PostgresRepository(
            second_store.pool,
            "approval_requests",
            "approval_request_id",
            ApprovalRequest,
        )
        now = datetime.now(UTC)
        pending, approved, rejected = _build_concurrent_approval_decisions(now)
        first.create(pending)

        def resolve(
            repository: PostgresRepository[ApprovalRequest], record: ApprovalRequest
        ) -> bool:
            return repository.put_if_status_and_stale(
                record,
                expected_status=ApprovalStatus.PENDING.value,
                # The repository's indexed updated_at column is set by the
                # database when a record is inserted, a few moments after
                # this client timestamp was captured.
                updated_before=now + timedelta(seconds=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(
                executor.map(
                    lambda args: resolve(*args),
                    ((first, approved), (second, rejected)),
                )
            )

        assert sorted(outcomes) == [False, True]
        assert first.get(pending.approval_request_id).status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
        }
    finally:
        second_store.close()
        first_store.close()


def _build_concurrent_approval_decisions(now):
    """Build one pending request and competing approved/rejected outcomes."""
    pending = ApprovalRequest(
        approval_request_id="approval-race",
        org_id="org-race",
        workspace_id="workspace-race",
        run_id="run-race",
        requested_by_principal_id="requester-race",
        capability_id="jira.issues.comment",
        action_summary="Comment on the release issue",
        risk_level=RiskLevel.MEDIUM,
        status=ApprovalStatus.PENDING,
        requested_at=now,
        created_at=now,
        updated_at=now,
    )
    resolved_at = now + timedelta(seconds=1)
    approved = _resolved_test_approval(
        pending, ApprovalStatus.APPROVED, "approver-a", resolved_at
    )
    rejected = _resolved_test_approval(
        pending, ApprovalStatus.REJECTED, "approver-b", resolved_at
    )
    return pending, approved, rejected


def _resolved_test_approval(pending, status, approver, resolved_at):
    """Copy a pending request into one terminal compare-and-set candidate."""
    return ApprovalRequest.model_validate(
        pending.model_copy(
            update={
                "status": status,
                "resolved_by_principal_id": approver,
                "resolved_at": resolved_at,
                "updated_at": resolved_at,
            }
        ).model_dump()
    )


def test_postgres_run_idempotency_is_shared_across_independent_pools(
    isolated_postgres_url,
):
    first_store = ProductPostgresStore(isolated_postgres_url)
    second_store = ProductPostgresStore(isolated_postgres_url)
    try:
        first = PostgresRepository(
            first_store.pool, "workflow_runs", "run_id", WorkflowRun
        )
        second = PostgresRepository(
            second_store.pool, "workflow_runs", "run_id", WorkflowRun
        )
        now = datetime.now(UTC)
        runs = tuple(
            WorkflowRun(
                run_id=f"run-idempotent-{index}",
                org_id="org-idempotent",
                workspace_id="workspace-idempotent",
                workflow_id="workflow-idempotent",
                workflow_version_id="workflow-version-idempotent",
                requested_by_principal_id="principal-idempotent",
                trigger_type="api.request",
                idempotency_key="request-idempotent",
                status=RunStatus.RUNNING,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            for index in range(2)
        )

        def create(
            repository: PostgresRepository[WorkflowRun], run: WorkflowRun
        ) -> bool:
            try:
                repository.create(run)
                return True
            except IdempotencyKeyAlreadyExists:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(create, (first, second), runs))

        assert sorted(outcomes) == [False, True]
        persisted = second.find_by_idempotency_key(
            "request-idempotent",
            "principal-idempotent",
            "org-idempotent",
            "workspace-idempotent",
        )
        assert persisted is not None
        assert persisted.run_id in {run.run_id for run in runs}
    finally:
        second_store.close()
        first_store.close()
