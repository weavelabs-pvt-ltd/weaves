"""Queue age reporting and round-robin state housekeeping for the worker."""

from __future__ import annotations

import logging
from datetime import timedelta

from application.ports.repository import Repository
from contracts.v1 import ExecutionJob, TaskTurn
from shared.clock import utc_now
from shared.request_context import system_scope

logger = logging.getLogger("product.worker")

# Round-robin history older than this cannot change who is served first.
CLAIM_STATE_RETENTION = timedelta(days=1)


class QueueHealthService:
    """Measure how long queued work has waited and prune claim history."""

    def __init__(
        self,
        *,
        execution_jobs: Repository[ExecutionJob],
        task_turns: Repository[TaskTurn],
        warn_seconds: int,
    ) -> None:
        """Bind both queues and the age that triggers a warning."""
        self._jobs = execution_jobs
        self._turns = task_turns
        self._warn_seconds = warn_seconds

    def oldest_queued_age_seconds(self) -> float | None:
        """Age of the longest-waiting queued job or turn; warn above the bound."""
        with system_scope("queue"):
            oldest = [
                moment
                for moment in (
                    self._jobs.oldest_queued_at(),
                    self._turns.oldest_queued_at(),
                )
                if moment is not None
            ]
        if not oldest:
            return None
        age = max(0.0, (utc_now() - min(oldest)).total_seconds())
        if age > self._warn_seconds:
            logger.warning(
                "oldest queued work has waited %.0fs (warn after %ds)",
                age,
                self._warn_seconds,
            )
        return age

    def prune_claim_state(self) -> int:
        """Drop round-robin history that no longer affects ordering."""
        with system_scope("queue"):
            return self._jobs.prune_claim_state(utc_now() - CLAIM_STATE_RETENTION)
