"""Slice 5.2 unit tests: in-memory fair claim parity, slots, bounded locks."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from application.ports.fair_claim import FairClaim, log_over_cap
from application.ports.limits import StaticLimitPolicy
from apps.worker.executor import (
    MAX_BACKOFF_SECONDS,
    SlotPool,
    backoff_seconds,
    slot_worker_id,
)
from apps.worker.loop import WorkerLoop
from apps.worker.main import effective_slots
from apps.worker.services import WorkerServices
from apps.worker.ticks import TickRegistry, build_tick_registry
from contracts.v1 import (
    ExecutionJob,
    ExecutionJobStatus,
    TaskTurn,
    TaskTurnStatus,
)
from infrastructure.persistence.product_repositories import (
    create_product_repository_set,
)
from modules.executions.queue_health import QueueHealthService
from shared.keyed_locks import KeyedLockRegistry
from shared.limits_config import LimitsConfig


def policy(cap: int = 2, mode: str = "enforce") -> StaticLimitPolicy:
    return StaticLimitPolicy(
        LimitsConfig(mode=mode, max_running_per_org=cap)  # type: ignore[arg-type]
    )


def memory_queues() -> tuple[Any, Any]:
    repositories = create_product_repository_set(None)
    return repositories.get("execution_jobs"), repositories.get("task_turns")


def job(job_id: str, org: str, age: int) -> ExecutionJob:
    when = datetime.now(UTC) - timedelta(seconds=age)
    return ExecutionJob(
        job_id=job_id,
        org_id=org,
        workspace_id="ws",
        requested_by_principal_id="p",
        task="t",
        status=ExecutionJobStatus.QUEUED,
        created_at=when,
        updated_at=when,
    )


def turn(turn_id: str, org: str, age: int) -> TaskTurn:
    when = datetime.now(UTC) - timedelta(seconds=age)
    return TaskTurn(
        turn_id=turn_id,
        task_id="task-" + turn_id,
        user_message_id="msg-" + turn_id,
        requested_by_principal_id="p",
        status=TaskTurnStatus.QUEUED,
        org_id=org,
        workspace_id="ws",
        created_at=when,
        updated_at=when,
    )


def test_memory_claim_is_round_robin_and_oldest_first_within_an_org() -> None:
    jobs, _ = memory_queues()
    for index in range(3):
        jobs.create(job(f"a{index}", "org-a", 100 - index))
    jobs.create(job("b0", "org-b", 1))  # newest row overall
    now = datetime.now(UTC)
    limits = policy(cap=10)
    claims = [jobs.claim_next_fair("w", now, limits).record.job_id for _ in range(4)]
    assert claims == ["a0", "b0", "a1", "a2"]
    assert jobs.claim_next_fair("w", now, limits) is None


def test_memory_cap_counts_both_queues_and_reports_observed_breach() -> None:
    jobs, turns = memory_queues()
    turns.create(turn("t1", "org-a", 10))
    jobs.create(job("j1", "org-a", 9))
    jobs.create(job("j2", "org-a", 8))
    now = datetime.now(UTC)
    enforce = policy(cap=2)
    assert turns.claim_next_fair("w", now, enforce) is not None
    assert jobs.claim_next_fair("w", now, enforce) is not None
    assert jobs.claim_next_fair("w", now, enforce) is None  # third is deferred
    observed = jobs.claim_next_fair("w", now, policy(cap=2, mode="observe"))
    assert observed is not None and observed.over_cap and observed.mode == "observe"


def test_memory_oldest_queued_and_prune() -> None:
    jobs, _ = memory_queues()
    assert jobs.oldest_queued_at() is None
    jobs.create(job("j", "org-a", 50))
    assert jobs.oldest_queued_at() is not None
    now = datetime.now(UTC)
    jobs.claim_next_fair("w", now, policy(cap=5))
    assert jobs.prune_claim_state(now + timedelta(seconds=1)) == 1
    assert jobs.prune_claim_state(now + timedelta(seconds=1)) == 0


def test_over_cap_is_logged_only_in_observe_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="product.worker")
    log_over_cap("execution job", "j1", FairClaim("rec", 2, 2, "observe"))
    log_over_cap("execution job", "j2", FairClaim("rec", 2, 2, "enforce"))
    log_over_cap("execution job", "j3", FairClaim("rec", 1, 2, "observe"))
    assert [r.getMessage().split("job_id=")[1].split()[0] for r in caplog.records] == [
        "j1"
    ]


def test_fair_claim_setting_defaults_on_and_parses() -> None:
    assert LimitsConfig().fair_claim is True
    assert LimitsConfig.from_env({"WEAVES_FAIR_CLAIM": "off"}).fair_claim is False
    assert LimitsConfig.from_env({"WEAVES_FAIR_CLAIM": ""}).fair_claim is True


def test_slot_ids_are_unique_and_bounded() -> None:
    assert slot_worker_id("host-abc", 0) == "host-abc-s1"
    long = slot_worker_id("x" * 200, 11)
    assert len(long) == 128 and long.endswith("-s12")


def test_backoff_is_exponential_and_bounded() -> None:
    assert backoff_seconds(1.0, 1) == 1.0
    assert backoff_seconds(1.0, 3) == 4.0
    assert backoff_seconds(1.0, 500) == MAX_BACKOFF_SECONDS


def test_slots_are_clamped_to_the_database_pool() -> None:
    assert effective_slots(4, 12) == 4
    assert effective_slots(4, 4) == 2
    assert effective_slots(4, 1) == 1


def test_slot_pool_runs_slots_concurrently_and_joins_in_flight_work() -> None:
    stopping = threading.Event()
    running = 0
    peak = 0
    guard = threading.Lock()
    finished: list[str] = []

    def work(worker_id: str) -> bool:
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        time.sleep(0.2)
        with guard:
            running -= 1
            finished.append(worker_id)
        stopping.set()
        return True

    pool = SlotPool(
        work, slots=3, base_worker_id="w", poll_seconds=0.01, stopping=stopping
    )
    pool.start()
    pool.join()
    assert peak == 3
    assert sorted(finished) == ["w-s1", "w-s2", "w-s3"]  # none cut short


def test_slot_survives_failures_with_backoff_and_a_dead_thread_is_revived() -> None:
    stopping = threading.Event()
    calls = {"n": 0}

    def work(worker_id: str) -> bool:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient")
        stopping.set()
        return False

    pool = SlotPool(
        work, slots=1, base_worker_id="w", poll_seconds=0.01, stopping=stopping
    )
    pool.start()
    pool.join()
    assert calls["n"] == 3

    other = threading.Event()
    revived = SlotPool(
        lambda _id: False,
        slots=1,
        base_worker_id="w",
        poll_seconds=0.01,
        stopping=other,
    )
    assert revived.revive() == 1  # never started counts as dead
    other.set()
    revived.join()


def make_services(
    log: list[tuple[str, str, Any]], busy: threading.Event | None = None
) -> WorkerServices:
    def claim_job(worker_id: str, limits: Any = None) -> Any:
        log.append(("job", worker_id, limits))
        return None

    def claim_turn(worker_id: str, limits: Any = None) -> Any:
        log.append(("turn", worker_id, limits))
        return None

    return cast(
        WorkerServices,
        SimpleNamespace(
            execution=SimpleNamespace(process_next_execution_job=claim_job),
            tasks=SimpleNamespace(process_next_turn=claim_turn),
        ),
    )


def test_claim_once_passes_limits_and_alternates_which_queue_goes_first() -> None:
    log: list[tuple[str, str, Any]] = []
    limits = policy()
    loop = WorkerLoop(
        make_services(log),
        TickRegistry(),
        worker_id="w",
        poll_seconds=0.01,
        stopping=threading.Event(),
        limits=limits,
        slots=2,
    )
    assert loop.claim_once("w-s1") is False
    assert loop.claim_once("w-s2") is False
    assert [entry[0] for entry in log] == ["job", "turn", "turn", "job"]
    assert all(entry[2] is limits for entry in log)


def test_supervisor_keeps_ticking_while_slots_are_busy_and_drains_on_stop() -> None:
    stopping = threading.Event()
    release = threading.Event()
    started = threading.Event()
    ticks: list[float] = []
    done: list[str] = []

    def slow_job(worker_id: str, limits: Any = None) -> Any:
        if started.is_set():
            return None
        started.set()
        release.wait(5)
        done.append(worker_id)
        return SimpleNamespace(
            job_id="j", status=SimpleNamespace(value="succeeded"), run_id="r"
        )

    services = cast(
        WorkerServices,
        SimpleNamespace(
            execution=SimpleNamespace(process_next_execution_job=slow_job),
            tasks=SimpleNamespace(process_next_turn=lambda w, limits=None: None),
        ),
    )
    registry = TickRegistry()
    from apps.worker.ticks import Tick

    registry.register(Tick("t", 0, lambda s: ticks.append(time.monotonic())))
    loop = WorkerLoop(
        services,
        registry,
        worker_id="w",
        poll_seconds=0.02,
        stopping=stopping,
        limits=policy(),
        slots=2,
    )
    thread = threading.Thread(target=loop.run)
    thread.start()
    assert started.wait(2)
    before = len(ticks)
    time.sleep(0.3)
    assert len(ticks) > before + 3  # ticks ran while a job was executing
    stopping.set()
    time.sleep(0.1)
    assert thread.is_alive()  # waits for the in-flight job
    release.set()
    thread.join(5)
    assert not thread.is_alive() and len(done) == 1


def test_default_registry_adds_queue_age_and_claim_state_ticks() -> None:
    names = [
        t.name
        for t in build_tick_registry(schedule_interval=5, lifecycle_interval=30).ticks
    ]
    assert {"queue_age", "claim_state_prune"} <= set(names)


def test_queue_health_reports_age_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    jobs, turns = memory_queues()
    health = QueueHealthService(execution_jobs=jobs, task_turns=turns, warn_seconds=60)
    assert health.oldest_queued_age_seconds() is None
    jobs.create(job("j", "org-a", 30))
    turns.create(turn("t", "org-a", 400))
    caplog.set_level(logging.WARNING, logger="product.worker")
    age = health.oldest_queued_age_seconds()
    assert age is not None and age >= 399
    assert "oldest queued work" in caplog.text


def test_keyed_locks_serialize_a_key_and_stay_bounded() -> None:
    registry = KeyedLockRegistry(max_entries=8)
    for index in range(1000):
        with registry.hold(("p", f"k{index}")):
            pass
    assert len(registry) <= 8
    counter = {"value": 0, "max": 0}

    def critical() -> None:
        with registry.hold(("p", "same")):
            counter["value"] += 1
            counter["max"] = max(counter["max"], counter["value"])
            time.sleep(0.001)
            counter["value"] -= 1

    threads = [threading.Thread(target=critical) for _ in range(16)]
    for item in threads:
        item.start()
    for item in threads:
        item.join()
    assert counter["max"] == 1


def test_keyed_locks_never_evict_a_lock_in_use() -> None:
    registry = KeyedLockRegistry(max_entries=1)
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def holder() -> None:
        with registry.hold("hot"):
            entered.set()
            release.wait(5)
            order.append("holder")

    thread = threading.Thread(target=holder)
    thread.start()
    entered.wait(2)
    for index in range(20):  # churn far beyond the bound while "hot" is held
        with registry.hold(f"other{index}"):
            pass

    def contender() -> None:
        with registry.hold("hot"):
            order.append("contender")

    second = threading.Thread(target=contender)
    second.start()
    time.sleep(0.1)
    release.set()
    thread.join()
    second.join()
    assert order == ["holder", "contender"]
    with pytest.raises(ValueError):
        KeyedLockRegistry(max_entries=0)
