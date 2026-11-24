"""Slice 5.2: several slots execute real (deterministic-model) turns end to end."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, cast

from cryptography.fernet import Fernet

from application.ports.limits import StaticLimitPolicy
from apps.worker.executor import SlotPool
from apps.worker.loop import WorkerLoop
from apps.worker.services import WorkerServices
from apps.worker.ticks import TickRegistry
from contracts.v1 import TaskTurnStatus
from shared.limits_config import LimitsConfig
from tests.support.runtime import LocalPlatformRuntime


def _assert_all_succeeded(runtime: Any, tasks: list[Any]) -> None:
    for task in tasks:
        stored = runtime.task_service.get_task(task.task_id)
        assert stored.latest_turn_status is TaskTurnStatus.SUCCEEDED
        roles = [m.role.value for m in runtime.task_service.list_messages(task.task_id)]
        assert roles == ["user", "assistant"]


def test_three_slots_run_real_turns_without_shared_state_errors(
    isolated_postgres_url, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", isolated_postgres_url)
    monkeypatch.setenv(
        "WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    runtime = LocalPlatformRuntime()
    try:
        runtime.bootstrap()
        tasks = [
            runtime.task_service.create_task(
                agent_id="local-assistant",
                title=f"slot task {index}",
                initial_message=f"Summarize the local security basics {index}.",
            )
            for index in range(9)
        ]
        errors: list[BaseException] = []
        claimed: list[str] = []
        guard = threading.Lock()
        real_turn = runtime.task_worker_service.process_next_turn

        def process_turn(worker_id: str, limits: Any = None) -> Any:
            try:
                turn = real_turn(worker_id, limits)
            except BaseException as exc:
                errors.append(exc)
                raise
            if turn is not None:
                with guard:
                    claimed.append(turn.turn_id)
            return turn

        services = cast(
            WorkerServices,
            SimpleNamespace(
                execution=SimpleNamespace(
                    process_next_execution_job=runtime.execution_worker_service.process_next_execution_job
                ),
                tasks=SimpleNamespace(process_next_turn=process_turn),
            ),
        )
        stopping = threading.Event()
        limits = StaticLimitPolicy(LimitsConfig(mode="enforce", max_running_per_org=3))
        loop = WorkerLoop(
            services,
            TickRegistry(),
            worker_id="slot-test",
            poll_seconds=0.05,
            stopping=stopping,
            limits=limits,
            slots=3,
        )
        pool = SlotPool(
            loop.claim_once,
            slots=3,
            base_worker_id="slot-test",
            poll_seconds=0.05,
            stopping=stopping,
        )
        pool.start()
        deadline = 60.0
        waiter = threading.Event()
        while len(claimed) < len(tasks) and deadline > 0:
            waiter.wait(0.2)
            deadline -= 0.2
        stopping.set()
        pool.join()

        assert not errors
        assert sorted(claimed) == sorted(set(claimed))  # no turn ran twice
        assert len(claimed) == len(tasks)
        _assert_all_succeeded(runtime, tasks)
    finally:
        runtime.close()
