"""Phase 5.4 on real PostgreSQL: index use at scale, parity and tenant isolation.

Set WEAVES_TEST_DATABASE_URL to a disposable database. The first group seeds
tens of thousands of rows across 50 organizations by SQL and asserts, with
``EXPLAIN``, that every bounded query this slice added is answered from an
index and not from a scan of the whole table. The second group exercises the
real services against a smaller set.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from infrastructure.database.bounded_queries_schema import BOUNDED_QUERIES_MIGRATION
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from infrastructure.persistence.overview_data import (
    PostgresOverviewData,
)

ORGS = 50
TASKS_PER_ORG = 400

_SEED = (
    # tasks: 400 per org, a tenth completed, a fiftieth private
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'tasks', 'task-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('task_id', 'task-'||o||'-'||n,
                  'status', CASE WHEN n %% 10 = 0 THEN 'completed' ELSE 'open' END,
                  'audience', CASE WHEN n %% 50 = 0 THEN 'private' ELSE 'workspace' END,
                  'audience_owner_principal_id', 'owner-'||o),
              now() - make_interval(mins => n), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, %(tasks)s) n""",
    # task turns: two per task, a few in flight, spread over three weeks
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'task_turns', 'turn-'||o||'-'||n||'-'||t, 'org-'||o, 'ws-'||o,
              jsonb_build_object('turn_id', 'turn-'||o||'-'||n||'-'||t,
                  'task_id', 'task-'||o||'-'||n,
                  'agent_run_id', 'arun-'||o||'-'||n||'-'||t,
                  'status', CASE WHEN n %% 97 = 0 AND t = 2 THEN 'running'
                                 WHEN n %% 89 = 0 AND t = 2 THEN 'queued'
                                 WHEN n %% 13 = 0 THEN 'failed' ELSE 'succeeded' END),
              now() - make_interval(hours => n + t), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, %(tasks)s) n,
            generate_series(1, 2) t""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'agent_runs', 'arun-'||o||'-'||n||'-'||t, 'org-'||o, 'ws-'||o,
              jsonb_build_object('agent_run_id', 'arun-'||o||'-'||n||'-'||t,
                  'run_id', 'run-'||o||'-'||n||'-'||t, 'status', 'succeeded',
                  'estimated_cost_usd', '0.0100'),
              now() - make_interval(hours => n + t), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, %(tasks)s) n,
            generate_series(1, 2) t""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'tool_invocations', 'inv-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('invocation_id', 'inv-'||o||'-'||n,
                  'run_id', 'run-'||o||'-'||n,
                  'status', CASE WHEN n = 7 THEN 'executing' ELSE 'succeeded' END),
              now() - make_interval(mins => n), now() - make_interval(hours => 9)
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 100) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'workflow_runs', 'wr-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('run_id', 'wr-'||o||'-'||n,
                  'status', CASE WHEN n = 3 THEN 'running' ELSE 'succeeded' END),
              now() - make_interval(mins => n), now() - make_interval(hours => 9)
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 100) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'execution_jobs', 'job-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('job_id', 'job-'||o||'-'||n,
                  'status', CASE WHEN n = 5 THEN 'running' ELSE 'succeeded' END),
              now() - make_interval(mins => n), now() - make_interval(hours => 9)
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 100) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'canvas_runs', 'cr-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('run_id', 'cr-'||o||'-'||n,
                  'workflow_id', 'wf-'||o||'-'||(n %% 5),
                  'status', CASE WHEN n = 2 THEN 'queued' ELSE 'succeeded' END),
              now() - make_interval(mins => n), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 100) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'canvas_settings', 'wf-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('workflow_id', 'wf-'||o||'-'||n, 'sched', n %% 4 = 0,
                  'next_run_at', to_char(now() + make_interval(days => n - 20),
                                         'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
              now(), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 40) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'users', 'user-'||o||'-'||n, 'org-'||o, NULL,
              jsonb_build_object('id', 'user-'||o||'-'||n,
                  'email', 'person'||n||'@org'||o||'.example',
                  'status', CASE WHEN n %% 20 = 0 THEN 'deleted' ELSE 'active' END),
              now(), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 200) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'audit_events', 'audit-'||o||'-'||n, 'org-'||o, 'ws-'||o,
              jsonb_build_object('audit_event_id', 'audit-'||o||'-'||n,
                  'action', CASE WHEN n = 1 THEN 'organization.created'
                                 ELSE 'task.created' END,
                  'target_id', CASE WHEN n = 1 THEN 'org-'||o
                                    ELSE 'task-'||o||'-'||n END),
              now() - make_interval(mins => n), now()
       FROM generate_series(1, %(orgs)s) o, generate_series(1, 300) n""",
)


class _Recorder:
    """Stand-in connection that records SQL and returns no rows."""

    def __init__(self, sink: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._sink = sink

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self._sink.append((sql, tuple(params)))
        return self

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> None:
        return None


class _RecordingPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    @contextmanager
    def connection(self):
        yield _Recorder(self.calls)


HEAVY = (
    # one heavy tenant (org-7) so per-tenant scans, not just table scans, show up
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'task_turns', 'heavy-turn-'||n, 'org-7', 'ws-7',
              jsonb_build_object('turn_id', 'heavy-turn-'||n,
                  'task_id', 'task-7-'||(n %% 400 + 1), 'status', 'succeeded'),
              now() - make_interval(secs => n), now()
       FROM generate_series(1, 20000) n""",
    """INSERT INTO product_records (collection, record_id, org_id, workspace_id,
           payload, created_at, updated_at)
       SELECT 'tool_invocations', 'heavy-inv-'||n, 'org-7', 'ws-7',
              jsonb_build_object('invocation_id', 'heavy-inv-'||n,
                  'run_id', 'heavy-run-'||n,
                  'status', 'succeeded'),
              now() - make_interval(secs => n), now()
       FROM generate_series(1, 20000) n""",
)


@pytest.fixture(scope="module")
def seeded():
    """Seed once per module in a private schema; read-only for every test."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    database_url = os.environ.get("WEAVES_TEST_DATABASE_URL", "").strip()
    if not database_url:
        pytest.skip("set WEAVES_TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"weaves_it_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    url = make_conninfo(database_url, options=f"-c search_path={schema},public")
    store = ProductPostgresStore(url)
    try:
        with store.pool.connection() as connection:
            for statement in (*_SEED, *HEAVY):
                connection.execute(statement, {"orgs": ORGS, "tasks": TASKS_PER_ORG})
            connection.execute("ANALYZE product_records")
        yield store
    finally:
        store.close()
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def _repo(collection: str, id_field: str, record_type: Any = object):
    from infrastructure.persistence.postgres_repository import PostgresRepository

    pool = _RecordingPool()
    return PostgresRepository(pool, collection, id_field, record_type), pool


def _plan(store: ProductPostgresStore, sql: str, params: tuple[Any, ...]) -> str:
    with store.pool.connection() as connection:
        rows = connection.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchall()
    return json.dumps(rows[0][0])


def _assert_indexed(
    store, repo_and_pool, expected_index: str, *, per_call: bool = True
):
    _, pool = repo_and_pool
    assert pool.calls, "the query under test issued no SQL"
    sql, params = pool.calls[-1]
    plan = _plan(store, sql, params)
    assert '"Node Type": "Seq Scan"' not in plan, plan
    names = (expected_index,) if isinstance(expected_index, str) else expected_index
    assert any(name in plan for name in names), plan


def test_migration_28_is_the_schema_head_and_creates_every_index(
    isolated_postgres_url,
):
    store = ProductPostgresStore(isolated_postgres_url)
    try:
        assert BOUNDED_QUERIES_MIGRATION[0] == 28 <= SCHEMA_HEAD_VERSION
        with store.pool.connection() as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'product_records'"
                ).fetchall()
            }
        created = {
            statement.split("IF NOT EXISTS")[1].split()[0]
            for statement in BOUNDED_QUERIES_MIGRATION[1]
        }
        assert len(created) == len(BOUNDED_QUERIES_MIGRATION[1]) == 19
        # Migration 31 dropped the indexes of the removed approvals queries.
        dropped = {
            "approval_requests_page_idx",
            "approval_requests_pending_idx",
            "tool_invocations_approval_idx",
        }
        assert created - dropped <= names
        assert not dropped & names
    finally:
        store.close()


def test_recovery_and_dispatch_queries_use_their_partial_indexes(seeded):
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    runs = _repo("workflow_runs", "run_id")
    runs[0].list_status_updated_before("running", cutoff, limit=200)
    _assert_indexed(seeded, runs, "workflow_runs_running_idx")
    jobs = _repo("execution_jobs", "job_id")
    jobs[0].list_status_updated_before("running", cutoff, limit=200)
    _assert_indexed(seeded, jobs, "execution_jobs_leased_idx")
    jobs[0].list_status_updated_before("cancel_requested", cutoff, limit=200)
    _assert_indexed(seeded, jobs, "execution_jobs_leased_idx")
    invocations = _repo("tool_invocations", "invocation_id")
    invocations[0].list_status_updated_before("executing", cutoff, limit=200)
    _assert_indexed(seeded, invocations, "tool_invocations_executing_idx")
    canvas = _repo("canvas_runs", "run_id")
    for state in ("queued", "running"):
        canvas[0].list_by_status(state, limit=200, after_id="cr-0")
        _assert_indexed(seeded, canvas, "canvas_runs_active_idx")
    settings = _repo("canvas_settings", "workflow_id")
    settings[0].list_before(
        "next_run_at", datetime.now(UTC), limit=200, equals={"sched": "true"}
    )
    _assert_indexed(seeded, settings, "canvas_settings_due_idx")


def test_point_lookups_replace_whole_tenant_scans(seeded):
    turns = _repo("task_turns", "turn_id")
    turns[0].list_by_field_in(
        "task_id",
        tuple(f"task-7-{n}" for n in range(1, 51)),
        org_id="org-7",
        workspace_id="ws-7",
    )
    _assert_indexed(seeded, turns, "task_turns_task_idx")
    agent_runs = _repo("agent_runs", "agent_run_id")
    agent_runs[0].get_many_scoped(
        tuple(f"arun-7-{n}-1" for n in range(1, 51)), "org-7", "ws-7"
    )
    _assert_indexed(seeded, agent_runs, "product_records_workspace_scope_idx")
    agent_runs[0].list_by_field_in(
        "run_id", ("run-7-9-1",), org_id="org-7", workspace_id="ws-7"
    )
    _assert_indexed(seeded, agent_runs, "agent_runs_run_idx")
    invocations = _repo("tool_invocations", "invocation_id")
    invocations[0].list_by_field_in(
        "run_id",
        tuple(f"run-7-{n}" for n in range(1, 51)),
        org_id="org-7",
        workspace_id="ws-7",
    )
    _assert_indexed(seeded, invocations, "tool_invocations_run_idx")
    users = _repo("users", "id")
    users[0].find_scoped_by_field(
        "email",
        "person5@org7.example",
        org_id="org-7",
        casefold=True,
        exclude_status="deleted",
    )
    _assert_indexed(seeded, users, "product_records_active_user_email_uq")
    audit = _repo("audit_events", "audit_event_id")
    audit[0].list_audit_page(
        "org-7",
        None,
        action="organization.created",
        target_type=None,
        target_id="org-7",
        actor_principal_id=None,
        created_after=None,
        created_before=None,
        cursor_created_at=None,
        cursor_event_id=None,
        limit=100,
    )
    _assert_indexed(seeded, audit, "audit_events_action_target_idx")


def test_list_pages_use_their_keyset_indexes(seeded):
    canvas = _repo("canvas_runs", "run_id")
    canvas[0].list_recent_page(
        "org-7",
        "ws-7",
        filters={"workflow_id": "wf-7-3"},
        cursor_created_at=None,
        cursor_id=None,
        limit=101,
    )
    _assert_indexed(seeded, canvas, "canvas_runs_workflow_page_idx")


def test_overview_statements_are_indexed_and_a_snapshot_is_a_few_statements(seeded):
    with seeded.pool.connection() as connection:
        active = _plan(
            seeded,
            """SELECT count(*) FROM product_records t
                   WHERE t.collection = 'task_turns' AND t.org_id = %s
                     AND t.workspace_id = %s
                     AND t.payload->>'status' IN ('queued', 'running')""",
            ("org-7", "ws-7"),
        )
        assert "task_turns_active_idx" in active
        recent = _plan(
            seeded,
            """SELECT count(*) FROM product_records t
                   WHERE t.collection = 'task_turns' AND t.org_id = %s
                     AND t.workspace_id = %s
                     AND t.created_at >= now() - interval '182 days'""",
            ("org-7", "ws-7"),
        )
        assert '"Node Type": "Seq Scan"' not in recent
        assert "org_id" in recent and "workspace_id" in recent
        del connection
    overview = PostgresOverviewData(seeded)
    first = overview.snapshot(
        "org-7",
        "ws-7",
        "owner-7",
        now=datetime.now(UTC),
        tz_offset_minutes=330,
    )
    assert first["tasks"]["total"] == TASKS_PER_ORG
    assert "pending_approvals" not in first
    # Only this tenant's turns are aggregated, not the 60k seeded in total.
    assert sum(day["total"] for day in first["daily"]) == 2 * TASKS_PER_ORG + 20_000
    stranger = overview.snapshot(
        "org-7",
        "ws-7",
        "someone-else",
        now=datetime.now(UTC),
        tz_offset_minutes=0,
    )
    assert stranger["tasks"]["total"] == TASKS_PER_ORG - TASKS_PER_ORG // 50
    other_org = overview.snapshot(
        "org-8",
        "ws-8",
        "owner-8",
        now=datetime.now(UTC),
        tz_offset_minutes=0,
    )
    assert other_org["tasks"]["total"] == TASKS_PER_ORG
    assert sum(day["total"] for day in other_org["daily"]) == 2 * TASKS_PER_ORG
    assert other_org["setup"] == {"has_agent": False, "has_active_connection": False}
