"""Organization purge is single-executor per organization across processes.

Two independent runtimes (separate pools, each under the restricted app role)
stand in for two worker processes sharing one database.
"""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.purge_lock import PostgresPurgeLock
from infrastructure.database.store import ProductPostgresStore
from shared.request_context import require_tenant_context
from tests.support.lifecycle_http import REQUEST, populated_world
from tests.support.operator_scripts import organization_lifecycle, run_script
from tests.support.organization_administration import headers

RETAINED = {"organization_lifecycle", "audit_events"}
STEPS = ("cancel_work", "revoke_credentials", "slack", "secrets", "usage")


def open_runtime(url, role="weaves_app"):
    store = ProductPostgresStore(
        pool=open_postgres_pool(url, min_size=1, max_size=6), app_role=role
    )
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


class Steps:
    """Counts every step one process executes, optionally parking or failing."""

    def __init__(self, service):
        self.counts = Counter()
        self.guard = threading.Lock()
        self.park_at = None
        self.fail_at = None
        self.reached = threading.Event()
        self.release = threading.Event()
        service.hook = self

    def __call__(self, name):
        with self.guard:
            self.counts[name] += 1
        if name == self.fail_at:
            self.fail_at = None
            raise RuntimeError("crash")
        if name == self.park_at:
            self.park_at = None
            self.reached.set()
            assert self.release.wait(timeout=20)


class Tracked:
    """Wraps a purge lock and records the most simultaneous holders per org."""

    def __init__(self, inner, shared=None):
        self.inner = inner
        self.active, self.peak, self.guard = shared or (
            Counter(),
            Counter(),
            threading.Lock(),
        )

    def hold(self, org_id):
        outer = self

        class Scope:
            def __enter__(self):
                self.cm = outer.inner.hold(org_id)
                self.acquired = self.cm.__enter__()
                if self.acquired:
                    with outer.guard:
                        outer.active[org_id] += 1
                        outer.peak[org_id] = max(
                            outer.peak[org_id], outer.active[org_id]
                        )
                return self.acquired

            def __exit__(self, *exc):
                if self.acquired:
                    with outer.guard:
                        outer.active[org_id] -= 1
                return self.cm.__exit__(*exc)

        return Scope()


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    url = isolated_postgres_url
    runtimes = {k: open_runtime(url) for k in ("api", "one", "two")}
    runtimes["operator"] = open_runtime(url, "")  # the owner role, as the CLI runs
    require_tenant_context(True)
    world = populated_world(runtimes["api"])
    world.update(runtimes, url=url)
    world["steps"] = tuple(
        Steps(runtimes[k].organization_purge_service) for k in ("one", "two")
    )
    try:
        with ExitStack() as stack:
            world["client"] = stack.enter_context(
                TestClient(create_app(runtimes["api"]), raise_server_exceptions=False)
            )
            yield world
    finally:
        require_tenant_context(False)
        for runtime in runtimes.values():
            runtime.close()


def sql(world, query, *params):
    with psycopg.connect(world["url"], autocommit=True) as connection:
        return connection.execute(query, params).fetchall()


def collections(world, org):
    rows = sql(
        world,
        "SELECT DISTINCT collection FROM product_records WHERE org_id = %s",
        world[org][0].id,
    )
    return {r[0] for r in rows}


class Out:
    def readouterr(self):
        return type("Out", (), {"out": "", "err": ""})()


def start_purge(world, org="a"):
    """Request deletion and skip the grace period, as the operator would."""
    token, name = ("owner_token", "Org A") if org == "a" else ("b_token", "Org B")
    response = world["client"].post(
        REQUEST,
        headers=headers(world, token=token, tenant=org),
        json={"confirm_name": name},
    )
    assert response.status_code == 200
    code = run_script(
        organization_lifecycle,
        Out(),
        world["operator"],
        "purge-now",
        "--organization-id",
        world[org][0].id,
        "--operator",
        "ops@example.test",
        "--reason",
        "pg test",
        "--confirm-organization-name",
        name,
    )[0]
    assert code == 0


def state(world, org="a"):
    return world["operator"].organization_lifecycle_service.state_of(world[org][0].id)


def lock_is_free(world, org="a"):
    """True when another session can take (so nobody holds) the purge lock."""
    name = f"organization-purge:{world[org][0].id}"
    with psycopg.connect(world["url"], autocommit=True) as connection:
        free = connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (name,)
        ).fetchone()[0]
        if free:
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (name,)
            )
        return free


def services(world):
    return [world[k].organization_purge_service for k in ("one", "two")]


def test_second_process_skips_while_first_holds_the_purge(world):
    start_purge(world)
    one, two = services(world)
    steps_one, steps_two = world["steps"]
    steps_one.park_at = "cancel_work"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(one.tick)
        assert steps_one.reached.wait(timeout=20)
        assert not lock_is_free(world)
        assert two.tick() == 0  # returns at once, having run no step
        assert sum(steps_two.counts.values()) == 0
        steps_one.release.set()
        assert first.result(timeout=30) == 1
    assert lock_is_free(world)


def test_overlapping_ticks_run_every_step_once_per_pass_and_finish(world):
    a = world["a"][0].id
    start_purge(world)
    shared = (Counter(), Counter(), threading.Lock())  # both processes, one view
    for service in services(world):
        service.lock = Tracked(service.lock, shared)
    for _ in range(60):
        barrier = threading.Barrier(2)

        def run(service, barrier=barrier):
            barrier.wait(timeout=10)
            return service.tick()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, services(world)))
        if state(world).value == "deleted":
            break
    assert state(world).value == "deleted"
    assert collections(world, "a") == RETAINED
    assert world["operator"].organization_purge_service.remaining_rows(a) == 0
    assert shared[1][a] == 1  # never two holders of one organization
    total = world["steps"][0].counts + world["steps"][1].counts
    passes = total["tombstone_started"]
    assert passes >= 3
    for name in STEPS:  # each step exactly once per pass, never twice at once
        assert total[name] == passes, name
    assert total["finalize"] == 1


def test_crash_mid_step_releases_the_lock_and_another_process_resumes(world):
    start_purge(world)
    steps_one, steps_two = world["steps"]
    steps_one.fail_at = "usage"
    one, two = services(world)
    one.tick()  # the failure is logged per organization
    assert steps_one.fail_at is None and lock_is_free(world)
    assert state(world).value == "deleting"
    for _ in range(12):
        two.tick()
        if state(world).value == "deleted":
            break
    assert state(world).value == "deleted"
    assert collections(world, "a") == RETAINED
    assert steps_two.counts["finalize"] == 1


def test_different_organizations_purge_without_blocking_each_other(world):
    start_purge(world, "a")
    start_purge(world, "b")
    one, two = services(world)
    steps_one = world["steps"][0]
    steps_one.park_at = "cancel_work"  # process one parks inside its first org
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(one.tick)
        assert steps_one.reached.wait(timeout=20)
        parked = [o for o in ("a", "b") if not lock_is_free(world, o)]
        assert len(parked) == 1
        other = "b" if parked == ["a"] else "a"
        for _ in range(12):  # process two finishes the other organization now
            two.tick()
            if state(world, other).value == "deleted":
                break
        assert state(world, other).value == "deleted"
        assert state(world, parked[0]).value == "deleting"
        steps_one.release.set()
        first.result(timeout=30)
    for _ in range(12):
        two.tick()
    assert state(world, "a").value == state(world, "b").value == "deleted"


def test_lock_is_try_only_and_released_on_error(world):
    store = ProductPostgresStore(
        pool=open_postgres_pool(world["url"], min_size=1, max_size=2)
    )
    lock = PostgresPurgeLock(store)
    org = world["a"][0].id
    try:
        with lock.hold(org) as first:
            assert first is True and not lock_is_free(world)
            with lock.hold(org) as second:  # same process, other connection
                assert second is False
            with lock.hold("another-org") as other:
                assert other is True
        assert lock_is_free(world)
        with pytest.raises(RuntimeError), lock.hold(org) as acquired:
            assert acquired
            raise RuntimeError("boom")
        assert lock_is_free(world)
        with lock.hold(org) as again:  # reusable after an error
            assert again is True
    finally:
        store.close()
