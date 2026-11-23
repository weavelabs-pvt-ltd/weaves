"""A bounded pool of claim-and-execute slots owned by the worker supervisor.

Each slot is a thread that repeatedly claims one unit of work and runs it to
completion. The supervisor (the worker's main thread) keeps running ticks
(recovery, schedule dispatch, studio, purges) while slots are busy, restarts a
slot thread that died, and on shutdown lets every in-flight execution finish
before returning, exactly as the single loop always did.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread

logger = logging.getLogger("product.worker")

# A slot whose iteration keeps failing backs off exponentially up to this bound.
MAX_BACKOFF_SECONDS = 30.0
MAX_WORKER_ID_LENGTH = 128


def slot_worker_id(base: str, index: int) -> str:
    """The lease owner name of one slot; unique per slot, at most 128 characters."""
    suffix = f"-s{index + 1}"
    return base[: MAX_WORKER_ID_LENGTH - len(suffix)] + suffix


def backoff_seconds(poll_seconds: float, failures: int) -> float:
    """Bounded exponential backoff after ``failures`` consecutive errors."""
    return min(
        MAX_BACKOFF_SECONDS, poll_seconds * float(2 ** min(16, max(0, failures - 1)))
    )


class SlotPool:
    """Run ``work(worker_id)`` in ``slots`` threads until ``stopping`` is set."""

    def __init__(
        self,
        work: Callable[[str], bool],
        *,
        slots: int,
        base_worker_id: str,
        poll_seconds: float,
        stopping: Event,
    ) -> None:
        """``work`` returns True when it did something (no idle wait needed)."""
        if slots < 1:
            raise ValueError("a worker needs at least one slot")
        self._work = work
        self._ids = [slot_worker_id(base_worker_id, i) for i in range(slots)]
        self._poll_seconds = poll_seconds
        self._stopping = stopping
        self._threads: list[Thread | None] = [None] * slots
        self.restarts = 0

    def start(self) -> None:
        """Start every slot."""
        for index in range(len(self._ids)):
            self._spawn(index)

    def revive(self) -> int:
        """Restart slot threads that died unexpectedly; return how many."""
        revived = 0
        for index, thread in enumerate(self._threads):
            if self._stopping.is_set():
                break
            if thread is None or not thread.is_alive():
                logger.error("worker slot died; restarting slot=%s", self._ids[index])
                self._spawn(index)
                self.restarts += 1
                revived += 1
        return revived

    def busy_or_alive(self) -> int:
        """Number of slot threads currently alive."""
        return sum(1 for t in self._threads if t is not None and t.is_alive())

    def join(self) -> None:
        """Wait for every slot to finish its in-flight work (no timeout)."""
        for thread in self._threads:
            if thread is not None:
                thread.join()

    def _spawn(self, index: int) -> None:
        thread = Thread(
            target=self._run_slot,
            args=(self._ids[index],),
            name=f"weaves-slot-{index + 1}",
            daemon=False,
        )
        self._threads[index] = thread
        thread.start()

    def _run_slot(self, worker_id: str) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                did_work = self._work(worker_id)
            except Exception:
                failures += 1
                logger.exception("worker slot iteration failed slot=%s", worker_id)
                self._stopping.wait(backoff_seconds(self._poll_seconds, failures))
                continue
            failures = 0
            if not did_work:
                self._stopping.wait(self._poll_seconds)
