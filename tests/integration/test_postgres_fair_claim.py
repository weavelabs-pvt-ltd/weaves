"""Slice 5.2: fair claiming, per-organization running cap, crash recovery."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from application.ports.limits import StaticLimitPolicy
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    TaskTurn,
    TaskTurnStatus,
)
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from shared.limits_config import LimitsConfig
from shared.request_context import system_scope


def policy(cap: int = 2, mode: str = "enforce") -> StaticLimitPolicy:
    return StaticLimitPolicy(
        LimitsConfig(mode=mode, max_running_per_org=cap)  # type: ignore[arg-type]
    )


class Env:
    """Two independent pools, like two worker processes."""

    def __init__(self, url: str) -> None:
        self.stores = [ProductPostgresStore(url), ProductPostgresStore(url)]
        sets = [create_product_repository_set(s) for s in self.stores]
        self.jobs: list[Any] = [s.get("execution_jobs") for s in sets]
        self.turns: list[Any] = [s.get("task_turns") for s in sets]
        self._n = 0

    def close(self) -> None:
        for store in self.stores:
            store.close()

    def add_job(self, org: str) -> str:
        self._n += 1
        job_id = f"job-{org}-{self._n:04d}"
        when = datetime.now(UTC) - timedelta(seconds=1000 - self._n)
        self.jobs[0].create(
            ExecutionJob(
                job_id=job_id,
                org_id=org,
                workspace_id=f"ws-{org}",
                requested_by_principal_id=f"p-{org}",
                task="work",
                status=ExecutionJobStatus.QUEUED,
                created_at=when,
                updated_at=when,
            )
        )
        return job_id

    def add_turn(self, org: str) -> str:
        self._n += 1
        turn_id = f"turn-{org}-{self._n:04d}"
        when = datetime.now(UTC) - timedelta(seconds=1000 - self._n)
        self.turns[0].create(
            TaskTurn(
                turn_id=turn_id,
                task_id=f"task-{turn_id}",
                user_message_id=f"msg-{turn_id}",
                requested_by_principal_id=f"p-{org}",
                status=TaskTurnStatus.QUEUED,
                org_id=org,
                workspace_id=f"ws-{org}",
                created_at=when,
                updated_at=when,
            )
        )
        return turn_id

    def claim_job(self, worker: str, limits: StaticLimitPolicy, side: int = 0) -> Any:
        with system_scope("queue"):
            return self.jobs[side].claim_next_fair(worker, datetime.now(UTC), limits)

    def claim_turn(self, worker: str, limits: StaticLimitPolicy, side: int = 0) -> Any:
        with system_scope("queue"):
            return self.turns[side].claim_next_fair(worker, datetime.now(UTC), limits)

    def running(self, org: str) -> int:
        with self.stores[0].pool.connection() as connection:
            row = connection.execute(
                "SELECT count(*) FROM product_records WHERE org_id = %s AND "
                "collection IN ('execution_jobs', 'task_turns') AND "
                "payload->>'status' = 'running'",
                (org,),
            ).fetchone()
        return int(row[0])


@pytest.fixture
def env(isolated_postgres_url):
    environment = Env(isolated_postgres_url)
    yield environment
    environment.close()


def test_a_backlogged_tenant_does_not_starve_a_late_one(env):
    for _ in range(20):
        env.add_job("org-a")
    late = env.add_job("org-b")  # newest row in the table
    limits = policy(cap=50)
    first = env.claim_job("w", limits)
    second = env.claim_job("w", limits)
    assert first is not None and first.record.org_id == "org-a"
    assert second is not None and second.record.job_id == late


def test_round_robin_serves_each_organization_in_turn(env):
    for org in ("org-a", "org-b", "org-c"):
        for _ in range(4):
            env.add_job(org)
    limits = policy(cap=50)
    served = [env.claim_job("w", limits).record.org_id for _ in range(9)]
    assert all(set(served[i : i + 3]) == {"org-a", "org-b", "org-c"} for i in (0, 3, 6))
    assert Counter(served) == {"org-a": 3, "org-b": 3, "org-c": 3}


def test_within_an_organization_the_oldest_row_goes_first(env):
    ids = [env.add_job("org-a") for _ in range(3)]
    limits = policy(cap=50)
    assert [env.claim_job("w", limits).record.job_id for _ in range(3)] == ids


def test_the_running_cap_counts_jobs_and_turns_together(env):
    env.add_turn("org-a")
    env.add_job("org-a")
    env.add_job("org-a")
    limits = policy(cap=2)
    assert env.claim_turn("w", limits).record.org_id == "org-a"
    assert env.claim_job("w", limits) is not None  # second running row
    assert env.claim_job("w", limits) is None  # third is over the cap
    other = env.add_job("org-b")
    assert env.claim_job("w", limits).record.job_id == other


def test_observe_mode_never_defers_but_reports_the_would_be_breach(env):
    for _ in range(3):
        env.add_job("org-a")
    limits = policy(cap=1, mode="observe")
    claims = [env.claim_job("w", limits) for _ in range(3)]
    assert all(claim is not None for claim in claims)
    assert [claim.over_cap for claim in claims] == [False, True, True]


def test_concurrent_claimers_never_exceed_the_cap_or_double_claim(env):
    for org in ("org-a", "org-b"):
        for _ in range(30):
            env.add_job(org)
        for _ in range(10):
            env.add_turn(org)
    limits = policy(cap=3)
    claimed: list[str] = []
    gate = threading.Barrier(12)
    guard = threading.Lock()

    def worker(index: int) -> None:
        gate.wait()
        for _ in range(10):
            claim = (
                env.claim_job(f"w{index}", limits, index % 2)
                if index % 3
                else env.claim_turn(f"w{index}", limits, index % 2)
            )
            if claim is not None:
                record = claim.record
                with guard:
                    claimed.append(getattr(record, "job_id", None) or record.turn_id)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(worker, range(12)))
    assert len(claimed) == len(set(claimed))  # nobody claimed a row twice
    # Nothing ever finished, so each organization sits at exactly its cap.
    assert env.running("org-a") == 3
    assert env.running("org-b") == 3
    assert len(claimed) == 6


def test_without_a_cap_two_pools_drain_every_row_exactly_once(env):
    expected = {env.add_job(f"org-{i % 4}") for i in range(60)}
    limits = policy(cap=1, mode="observe")
    seen: list[str] = []
    guard = threading.Lock()

    def drain(side: int) -> None:
        while (claim := env.claim_job(f"w{side}", limits, side)) is not None:
            with guard:
                seen.append(claim.record.job_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(drain, [0, 1] * 4))
    assert sorted(seen) == sorted(expected)


def test_a_killed_workers_slot_returns_after_lease_recovery(env, isolated_postgres_url):
    from tests.support.runtime import LocalPlatformRuntime

    first = env.add_job("org-a")
    env.add_job("org-a")
    limits = policy(cap=1)
    claim = env.claim_job("doomed-worker", limits)
    assert claim is not None and claim.record.job_id == first
    assert env.claim_job("other-worker", limits) is None  # slot is held

    # The worker is killed: its row stays running until the lease goes stale.
    stale = datetime.now(UTC) - timedelta(minutes=30)
    with env.stores[0].connection() as connection:
        connection.execute(
            "UPDATE product_records SET updated_at = %s, payload = payload || "
            "jsonb_build_object('updated_at', %s::text) WHERE record_id = %s",
            (stale, stale.isoformat(), first),
        )
    runtime = LocalPlatformRuntime(
        postgres_store=ProductPostgresStore(isolated_postgres_url)
    )
    try:
        with system_scope("recovery"):
            recovered = runtime.recover_stale_execution_jobs(stale_after_seconds=60)
        assert first in recovered
    finally:
        runtime.close()
    nxt = env.claim_job("other-worker", limits)
    assert nxt is not None and nxt.record.job_id != first


def test_claim_state_is_private_to_the_queue_scope_and_prunable(
    isolated_postgres_url,
):
    store = ProductPostgresStore(isolated_postgres_url, app_role="weaves_app")
    try:
        jobs = create_product_repository_set(store).get("execution_jobs")
        with store.pool.connection() as connection:
            connection.execute(
                "INSERT INTO tenant_scheduler_state VALUES ('org-a', now()), "
                "('org-b', now() - interval '3 days')"
            )
        with store.connection() as connection:  # tenant scope, no queue purpose
            assert not connection.execute(
                "SELECT 1 FROM tenant_scheduler_state"
            ).fetchall()
        with system_scope("queue"):
            assert jobs.prune_claim_state(datetime.now(UTC) - timedelta(days=1)) == 1
            with store.connection() as connection:
                rows = connection.execute(
                    "SELECT org_id FROM tenant_scheduler_state"
                ).fetchall()
        assert [row[0] for row in rows] == ["org-a"]
    finally:
        store.close()


def test_oldest_queued_at_reports_the_longest_wait(env):
    with system_scope("queue"):
        assert env.jobs[0].oldest_queued_at() is None
    env.add_job("org-a")
    with system_scope("queue"):
        assert env.jobs[0].oldest_queued_at() is not None


def test_running_count_uses_the_per_organization_partial_index(env):
    env.add_job("org-a")
    with env.stores[0].pool.connection() as connection:
        connection.execute("SET enable_seqscan = off")
        plan = "\n".join(
            row[0]
            for row in connection.execute(
                "EXPLAIN SELECT count(*) FROM product_records "
                "WHERE collection = 'execution_jobs' AND org_id = 'org-a' "
                "AND payload->>'status' IN ('running', 'cancel_requested')"
            ).fetchall()
        )
    assert "product_records_execution_job_org_running_idx" in plan
