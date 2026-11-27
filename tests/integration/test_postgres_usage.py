"""The usage ledger on PostgreSQL under the restricted application role."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import httpx
import psycopg
import pytest

from application.ports.usage import (
    UsageAdmission,
    UsageReservationRequest,
    UsageSettlement,
)
from application.task_run_keys import planner_usage_key
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import UsageGroupBy, UsageState
from infrastructure.database import store as store_module
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from infrastructure.database.usage_schema import USAGE_MIGRATION
from infrastructure.persistence.usage_ledger import PostgresUsageLedger
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support.operator_scripts import run_script, usage_report

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
TODAY = NOW.date()
OTHER_PURPOSES = (
    "bootstrap",
    "auth",
    "queue",
    "dispatch",
    "recovery",
    "studio",
    "account",
    "lifecycle",
    "catalog",
    "oauth",
    "waitlist",
)


def reserve_request(key, org="org-a", tokens=(100, 50), cost=None, now=NOW):
    return UsageReservationRequest(
        org_id=org,
        workspace_id="ws",
        reservation_key=key,
        agent_run_id=key,
        run_id="wf-" + key,
        agent_id="agent-1",
        provider_id="prov",
        model_id="model-x",
        reserved_input_tokens=tokens[0],
        reserved_output_tokens=tokens[1],
        reserved_cost_nano_usd=cost,
        now=now,
    )


def settle_request(key, org="org-a", tokens=(10, 5), cost=1_000, now=NOW):
    return UsageSettlement(
        org_id=org,
        workspace_id="ws",
        reservation_key=key,
        agent_run_id=key,
        run_id="wf-" + key,
        agent_id="agent-1",
        provider_id="prov",
        model_id="model-x",
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        estimated_cost_nano_usd=cost,
        outcome="ok",
        now=now,
    )


def admission(tokens_per_day, enforce=True):
    return UsageAdmission(
        day_start=NOW.replace(hour=0),
        month_start=NOW.replace(day=1, hour=0),
        tokens_per_day=tokens_per_day,
        tokens_per_month=10**9,
        cost_nano_usd_per_day=None,
        enforce=enforce,
    )


def as_tenant(org):
    return set_request_identity("principal", org, "ws")


@pytest.fixture
def store(isolated_postgres_url):
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=12)
    store = ProductPostgresStore(pool=pool, app_role="weaves_app")
    yield store
    store.close()
    require_tenant_context(False)


@pytest.fixture
def ledger(store):
    return PostgresUsageLedger(store)


def owner_sql(url, query, *params):
    with psycopg.connect(url, autocommit=True) as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def test_migration_27_creates_secured_tables_idempotently(isolated_postgres_url):
    assert SCHEMA_HEAD_VERSION >= 27 and USAGE_MIGRATION[0] == 27
    ProductPostgresStore(isolated_postgres_url).close()
    ProductPostgresStore(isolated_postgres_url).close()  # second start: no-op
    rows = owner_sql(
        isolated_postgres_url,
        "SELECT relname, relrowsecurity FROM pg_class "
        "WHERE relname IN ('usage_ledger', 'usage_daily') "
        "AND relnamespace = current_schema()::regnamespace ORDER BY relname",
    )
    assert rows == [("usage_daily", True), ("usage_ledger", True)]
    policies = owner_sql(
        isolated_postgres_url,
        "SELECT tablename, policyname FROM pg_policies "
        "WHERE tablename IN ('usage_ledger', 'usage_daily') "
        "AND schemaname = current_schema() ORDER BY 1",
    )
    assert policies == [
        ("usage_daily", "tenant_isolation"),
        ("usage_ledger", "tenant_isolation"),
    ]
    with psycopg.connect(isolated_postgres_url, autocommit=True) as connection:
        for statement in USAGE_MIGRATION[1]:
            connection.execute(statement)  # re-running by hand changes nothing


def test_migration_27_applies_over_a_database_at_version_24(
    isolated_postgres_url, monkeypatch
):
    previous = tuple(m for m in store_module._SCHEMA_MIGRATIONS if m[0] < 27)
    monkeypatch.setattr(store_module, "_SCHEMA_MIGRATIONS", previous)
    monkeypatch.setattr(
        store_module, "KNOWN_SCHEMA_VERSIONS", frozenset(m[0] for m in previous) | {20}
    )
    ProductPostgresStore(isolated_postgres_url).close()
    monkeypatch.undo()
    assert not owner_sql(
        isolated_postgres_url,
        "SELECT 1 FROM pg_tables WHERE tablename = 'usage_ledger' "
        "AND schemaname = current_schema()",
    )
    ProductPostgresStore(isolated_postgres_url).close()
    versions = [
        r[0]
        for r in owner_sql(
            isolated_postgres_url,
            "SELECT version FROM product_schema_migrations ORDER BY version",
        )
    ]
    assert 27 in versions and owner_sql(
        isolated_postgres_url,
        "SELECT 1 FROM pg_tables WHERE tablename = 'usage_ledger' "
        "AND schemaname = current_schema()",
    )


def test_a_tenant_sees_and_changes_only_its_own_usage(store, ledger):
    for org in ("org-a", "org-b"):
        token = as_tenant(org)
        try:
            ledger.reserve(reserve_request("run-1", org=org), None)
            ledger.settle(settle_request("run-1", org=org))
        finally:
            reset_request_principal(token)
    token = as_tenant("org-a")
    try:
        assert ledger.get("org-a", "run-1").state is UsageState.SETTLED
        assert ledger.get("org-b", "run-1") is None  # same key, other tenant
        with store.connection() as connection:
            assert not connection.execute(
                "SELECT 1 FROM usage_ledger WHERE org_id = 'org-b'"
            ).fetchall()
            assert not connection.execute(
                "SELECT 1 FROM usage_daily WHERE org_id = 'org-b'"
            ).fetchall()
            for table in ("usage_ledger", "usage_daily"):
                assert (
                    connection.execute(
                        f"UPDATE {table} SET org_id = org_id WHERE org_id = 'org-b'"
                    ).rowcount
                    == 0
                )
                assert (
                    connection.execute(
                        f"DELETE FROM {table} WHERE org_id = 'org-b'"
                    ).rowcount
                    == 0
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ledger.settle(settle_request("planted", org="org-b"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ledger.reserve(reserve_request("planted", org="org-b"), None)
        assert ledger.summary("org-b", TODAY, TODAY, UsageGroupBy.DAY)[0].calls == 0
        assert ledger.summary("org-a", TODAY, TODAY, UsageGroupBy.DAY)[0].calls == 1
    finally:
        reset_request_principal(token)


def test_no_identity_and_unrelated_system_purposes_see_no_usage(store, ledger):
    token = as_tenant("org-a")
    try:
        ledger.reserve(reserve_request("run-1"), None)
        ledger.settle(settle_request("run-1"))
    finally:
        reset_request_principal(token)
    require_tenant_context(True)  # production posture: no implicit local tenant
    for purpose in OTHER_PURPOSES:
        with system_scope(purpose), store.connection() as connection:
            for table in ("usage_ledger", "usage_daily"):
                assert not connection.execute(f"SELECT 1 FROM {table}").fetchall()
    with store.connection() as connection:
        assert not connection.execute("SELECT 1 FROM usage_ledger").fetchall()
    with system_scope("usage"):
        assert len(ledger.list_reserved_before(NOW + timedelta(days=1), limit=5)) == 0
        rows = ledger.operator_report(TODAY, TODAY, by="month", org_id=None)
    assert [(r.org_id, r.totals.calls) for r in rows] == [("org-a", 1)]


def test_replayed_reserve_and_settle_never_double_count(store, ledger):
    token = as_tenant("org-a")
    try:
        first = ledger.reserve(reserve_request("run-1"), admission(10**6))
        again = ledger.reserve(reserve_request("run-1"), admission(10**6))
        assert first.created and not again.created and first.event == again.event
        assert ledger.settle(settle_request("run-1")) is True
        assert ledger.settle(settle_request("run-1")) is False
        assert ledger.settle(settle_request("run-1", tokens=(999, 999))) is False
        assert not ledger.release("org-a", "run-1", outcome="x", now=NOW)
        assert not ledger.mark_unknown("org-a", "run-1", outcome="x", now=NOW)
        totals, groups, _, _ = ledger.summary("org-a", TODAY, TODAY, UsageGroupBy.AGENT)
    finally:
        reset_request_principal(token)
    assert (totals.calls, totals.input_tokens, totals.output_tokens) == (1, 10, 5)
    assert totals.estimated_cost_nano_usd == 1_000 and len(groups) == 1


def test_planner_rows_beside_run_rows_are_idempotent_and_fit_the_key_column(
    store, ledger
):
    """The Slack planner's ``{run key}:plan`` row (up to 128 characters)."""
    token = as_tenant("org-a")
    long_plan = planner_usage_key("t" * 200)  # shortened with a digest, never cut
    try:
        for key in ("run-1", "run-1:plan", long_plan):
            request = replace(reserve_request(key), run_id=key[:128])
            assert ledger.reserve(request, admission(10**6)).created
            assert not ledger.reserve(request, None).created
        assert len(long_plan) == 128
        assert ledger.settle(settle_request("run-1:plan", tokens=(480, 40))) is True
        assert ledger.settle(settle_request("run-1:plan", tokens=(9, 9))) is False
        long_settle = replace(
            settle_request(long_plan, tokens=(300, 30)), run_id=long_plan
        )
        assert ledger.settle(long_settle) is True
        assert ledger.get("org-a", "run-1").state is UsageState.RESERVED
        assert ledger.settle(settle_request("run-1", tokens=(1000, 200))) is True
        totals, _, _, _ = ledger.summary("org-a", TODAY, TODAY, UsageGroupBy.AGENT)
    finally:
        reset_request_principal(token)
    assert (totals.calls, totals.input_tokens, totals.output_tokens) == (3, 1780, 270)


def test_concurrent_settles_of_one_call_count_once(store, ledger):
    token = as_tenant("org-a")
    try:
        ledger.reserve(reserve_request("run-1"), None)
        barrier = Barrier(8)

        def settle():
            inner = as_tenant("org-a")  # worker threads do not inherit the context
            try:
                barrier.wait(timeout=10)
                return ledger.settle(settle_request("run-1"))
            finally:
                reset_request_principal(inner)

        with ThreadPoolExecutor(8) as pool:
            outcomes = list(pool.map(lambda _: settle(), range(8)))
        totals = ledger.summary("org-a", TODAY, TODAY, UsageGroupBy.DAY)[0]
    finally:
        reset_request_principal(token)
    assert outcomes.count(True) == 1 and totals.calls == 1 and totals.total_tokens == 15


def test_concurrent_reservations_at_the_cap_never_exceed_it(
    store, ledger, isolated_postgres_url
):
    cap = 1_000
    barrier = Barrier(20)

    def reserve(index):
        token = as_tenant("org-a")
        try:
            barrier.wait(timeout=10)
            return ledger.reserve(
                reserve_request(f"run-{index}", tokens=(100, 50)), admission(cap)
            )
        finally:
            reset_request_principal(token)

    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(reserve, range(20)))
    created = [r for r in results if r.created]
    refused = [r for r in results if r.event is None]
    assert len(created) == 6 and len(refused) == 14  # 6 x 150 = 900 <= 1000
    assert all(r.breach == "tokens_per_day" for r in refused)
    total = owner_sql(
        isolated_postgres_url,
        "SELECT sum(reserved_input_tokens + reserved_output_tokens) FROM usage_ledger",
    )[0][0]
    assert int(total) <= cap


def test_settled_usage_frees_reserved_headroom_and_caps_are_per_organization(
    store, ledger
):
    token = as_tenant("org-a")
    try:
        for index in range(6):
            assert ledger.reserve(
                reserve_request(f"run-{index}"), admission(1_000)
            ).created
        assert ledger.reserve(reserve_request("extra"), admission(1_000)).event is None
        ledger.settle(settle_request("run-0", tokens=(5, 5)))
        assert ledger.reserve(reserve_request("extra"), admission(1_000)).created
    finally:
        reset_request_principal(token)
    token = as_tenant("org-b")
    try:
        assert ledger.reserve(
            reserve_request("run-0", org="org-b"), admission(1_000)
        ).created
    finally:
        reset_request_principal(token)


def test_reservation_states_unknown_release_and_late_settlement(store, ledger):
    token = as_tenant("org-a")
    try:
        for key in ("lost", "unused"):
            ledger.reserve(reserve_request(key), None)
        assert ledger.mark_unknown("org-a", "lost", outcome="lease_expired", now=NOW)
        assert ledger.release("org-a", "unused", outcome="model.disabled", now=NOW)
        assert ledger.get("org-a", "lost").state is UsageState.UNKNOWN
        assert ledger.get("org-a", "unused").state is UsageState.RELEASED
        assert ledger.summary("org-a", TODAY, TODAY, UsageGroupBy.DAY)[2] == 1
        assert ledger.settle(settle_request("lost", tokens=(7, 3)))
        assert not ledger.settle(settle_request("lost", tokens=(7, 3)))
        totals = ledger.summary("org-a", TODAY, TODAY, UsageGroupBy.DAY)[0]
        assert totals.calls == 1 and totals.total_tokens == 10
    finally:
        reset_request_principal(token)


def test_purge_organization_removes_ledger_and_rollup_of_that_tenant_only(
    store, ledger, isolated_postgres_url
):
    for org in ("org-a", "org-b"):
        token = as_tenant(org)
        try:
            for index in range(3):
                ledger.reserve(reserve_request(f"run-{index}", org=org), None)
                ledger.settle(settle_request(f"run-{index}", org=org))
        finally:
            reset_request_principal(token)
    token = as_tenant("org-a")
    try:
        assert ledger.remaining_rows() == 4  # 3 ledger rows + 1 rollup row
        assert ledger.purge_organization(2) == 2
        assert ledger.purge_organization(100) == 2
        assert ledger.remaining_rows() == 0
    finally:
        reset_request_principal(token)
    left = owner_sql(
        isolated_postgres_url,
        "SELECT org_id, count(*) FROM usage_ledger GROUP BY 1",
    )
    assert left == [("org-b", 3)]
    assert owner_sql(isolated_postgres_url, "SELECT org_id FROM usage_daily") == [
        ("org-b",)
    ]


def test_retention_deletes_old_finished_rows_of_live_organizations(store, ledger):
    old = NOW - timedelta(days=500)
    token = as_tenant("org-a")
    try:
        ledger.reserve(reserve_request("old", now=old), None)
        ledger.settle(settle_request("old", now=old))
        ledger.reserve(reserve_request("open", now=old), None)
        ledger.reserve(reserve_request("new"), None)
        ledger.settle(settle_request("new"))
    finally:
        reset_request_principal(token)
    with system_scope("usage"):
        removed = ledger.purge_before(date(2026, 1, 1), 100)
    assert removed == 2  # the old settled row and its daily rollup
    token = as_tenant("org-a")
    try:
        assert ledger.get("org-a", "old") is None
        assert ledger.get("org-a", "open") is not None  # still reserved
        assert ledger.get("org-a", "new") is not None
    finally:
        reset_request_principal(token)


def test_operator_report_and_script_group_by_organization_and_period(
    isolated_postgres_url, store, ledger, capsys
):
    for org, day in (("org-a", 0), ("org-a", 1), ("org-b", 0)):
        token = as_tenant(org)
        try:
            when = NOW + timedelta(days=day)
            key = f"run-{org}-{day}"
            ledger.reserve(reserve_request(key, org=org, now=when), None)
            ledger.settle(settle_request(key, org=org, now=when, cost=2_500_000_000))
        finally:
            reset_request_principal(token)
    owner = compose_platform_runtime(
        http_client=httpx.Client(),
        store=ProductPostgresStore(isolated_postgres_url, app_role=""),
        seed_local_defaults=False,
    )
    try:
        status, out, logs = run_script(
            usage_report,
            capsys,
            owner,
            "--by",
            "month",
            "--from",
            "2026-11-01",
            "--to",
            "2026-11-30",
        )
        rows = json.loads(out)
        assert status == 0 and logs[-1]["event"] == "operator.usage_report"
        assert [(r["org_id"], r["calls"], r["estimated_cost_usd"]) for r in rows] == [
            ("org-a", 2, "5.000000000"),
            ("org-b", 1, "2.500000000"),
        ]
        assert rows[0]["input_tokens"] == 20 and rows[0]["period"] == "2026-11"
        _, out, _ = run_script(
            usage_report, capsys, owner, "--by", "day", "--org", "org-a",
            "--from", "2026-11-24", "--to", "2026-11-25", "--csv",
        )  # fmt: skip
        lines = out.strip().splitlines()
        assert lines[0].startswith("period,org_id,org_name,calls")
        assert [line.split(",")[0] for line in lines[1:]] == [
            "2026-11-24",
            "2026-11-25",
        ]
        assert run_script(usage_report, capsys, owner, "--from", "2026-12-01", "--to",
                          "2026-11-01")[0] == 2  # fmt: skip
    finally:
        owner.close()
