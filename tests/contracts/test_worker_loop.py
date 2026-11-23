"""Phase 5.0: the worker tick registry and loop keep the original semantics."""

from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest

from apps.worker.loop import WorkerLoop
from apps.worker.services import WorkerServices
from apps.worker.ticks import Tick, TickRegistry, build_tick_registry


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_services(
    *, job: Any = None, turn: Any = None, calls: list[str] | None = None
) -> WorkerServices:
    log = calls if calls is not None else []

    def claim_job(worker_id: str) -> Any:
        log.append("job")
        return job

    def claim_turn(worker_id: str) -> Any:
        log.append("turn")
        return turn

    return cast(
        WorkerServices,
        SimpleNamespace(
            execution=SimpleNamespace(process_next_execution_job=claim_job),
            tasks=SimpleNamespace(process_next_turn=claim_turn),
        ),
    )


def make_loop(
    registry: TickRegistry,
    services: WorkerServices | None = None,
    clock: Clock | None = None,
    stopping: Event | None = None,
) -> WorkerLoop:
    return WorkerLoop(
        services or make_services(),
        registry,
        worker_id="w1",
        poll_seconds=0.01,
        stopping=stopping or Event(),
        clock=clock or Clock(),
    )


def test_registry_rejects_duplicates_and_negative_intervals() -> None:
    registry = TickRegistry()
    registry.register(Tick("a", 1, lambda s: None))
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(Tick("a", 1, lambda s: None))
    with pytest.raises(ValueError, match="negative"):
        registry.register(Tick("b", -1, lambda s: None))
    assert [tick.name for tick in registry.ticks] == ["a"]


def test_default_registry_keeps_the_original_order_and_intervals() -> None:
    registry = build_tick_registry(schedule_interval=5, lifecycle_interval=30)
    assert [(t.name, t.interval_seconds, t.run_at_start) for t in registry.ticks] == [
        ("recovery", 30, False),
        ("organization_purge", 30, True),
        ("waitlist_purge", 3600, True),
        ("schedule_dispatch", 5, True),
        ("studio", 0, True),
        ("recovery_sweep", 86_400, False),
    ]


def test_ticks_run_in_order_when_due_and_respect_intervals() -> None:
    order: list[str] = []
    registry = TickRegistry()
    registry.register(Tick("slow", 10, lambda s: order.append("slow")))
    registry.register(Tick("every", 0, lambda s: order.append("every")))
    delayed = Tick("delayed", 10, lambda s: order.append("delayed"), run_at_start=False)
    registry.register(delayed)
    clock = Clock()
    loop = make_loop(registry, clock=clock)
    loop.run_ticks()
    assert order == ["slow", "every"]
    order.clear()
    clock.now += 5
    loop.run_ticks()
    assert order == ["every"]
    order.clear()
    clock.now += 5
    loop.run_ticks()
    assert order == ["slow", "every", "delayed"]


def test_failing_tick_propagates_and_is_retried_next_iteration() -> None:
    attempts: list[int] = []

    def flaky(services: WorkerServices) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")

    registry = TickRegistry()
    registry.register(Tick("flaky", 100, flaky))
    calls: list[str] = []
    loop = make_loop(registry, make_services(calls=calls))
    with pytest.raises(RuntimeError):
        loop.iterate()
    assert calls == []  # nothing is claimed after a tick failure
    loop.iterate()
    assert len(attempts) == 2 and calls == ["job", "turn"]


def test_claim_skips_the_turn_when_stopping_and_reports_work() -> None:
    stopping = Event()
    job = SimpleNamespace(
        job_id="j", status=SimpleNamespace(value="succeeded"), run_id="r"
    )
    calls: list[str] = []
    loop = make_loop(
        TickRegistry(), make_services(job=job, calls=calls), stopping=stopping
    )
    stopping.set()
    assert loop.claim_and_run() is True
    assert calls == ["job"]
    idle = make_loop(TickRegistry(), make_services(calls=calls))
    assert idle.claim_and_run() is False


def test_run_waits_when_idle_and_survives_iteration_errors() -> None:
    stopping = Event()
    seen = {"iterations": 0}

    def tick(services: WorkerServices) -> None:
        seen["iterations"] += 1
        if seen["iterations"] == 1:
            raise RuntimeError("transient")
        if seen["iterations"] == 3:
            stopping.set()

    registry = TickRegistry()
    registry.register(Tick("t", 0, tick))
    make_loop(registry, stopping=stopping).run()
    assert seen["iterations"] == 3
