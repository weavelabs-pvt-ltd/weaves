"""The worker's polling loop, separated from process setup so it can be tested."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from threading import Event

from apps.worker.services import WorkerServices
from apps.worker.ticks import TickRegistry

logger = logging.getLogger("product.worker")


class WorkerLoop:
    """Run registered ticks, then claim at most one job and one task turn."""

    def __init__(
        self,
        services: WorkerServices,
        registry: TickRegistry,
        *,
        worker_id: str,
        poll_seconds: float,
        stopping: Event,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind services and ticks; ``clock`` is injectable for tests."""
        self._services = services
        self._registry = registry
        self._worker_id = worker_id
        self._poll_seconds = poll_seconds
        self._stopping = stopping
        self._clock = clock
        start = clock()
        # ``None`` means due immediately; otherwise the monotonic time last run.
        self._last_run: dict[str, float | None] = {
            tick.name: None if tick.run_at_start else start for tick in registry.ticks
        }

    def run_ticks(self) -> None:
        """Run every due tick in registration order; exceptions propagate."""
        for tick in self._registry.ticks:
            last = self._last_run[tick.name]
            if last is not None and self._clock() - last < tick.interval_seconds:
                continue
            tick.run(self._services)
            self._last_run[tick.name] = self._clock()

    def claim_and_run(self) -> bool:
        """Process the next job and, unless stopping, the next turn.

        Returns True when any work was done. Slice 5.2 replaces this step.
        """
        services = self._services
        job = services.execution.process_next_execution_job(self._worker_id)
        turn = (
            None
            if self._stopping.is_set()
            else services.tasks.process_next_turn(self._worker_id)
        )
        if turn is not None:
            logger.info(
                "task turn finished turn_id=%s status=%s",
                turn.turn_id,
                turn.status.value,
            )
        if job is not None:
            logger.info(
                "product execution job finished job_id=%s status=%s run_id=%s",
                job.job_id,
                job.status.value,
                job.run_id,
            )
        return job is not None or turn is not None

    def iterate(self) -> bool:
        """One loop pass. Returns True when work was done (no idle wait needed)."""
        self.run_ticks()
        return self.claim_and_run()

    def run(self) -> None:
        """Loop until ``stopping`` is set, preserving the original error handling."""
        while not self._stopping.is_set():
            try:
                if not self.iterate():
                    self._stopping.wait(self._poll_seconds)
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("product execution worker iteration failed")
                self._stopping.wait(self._poll_seconds)
