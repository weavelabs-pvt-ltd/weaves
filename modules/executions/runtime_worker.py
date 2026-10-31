"""Execution job lifecycle capability use cases."""

from __future__ import annotations

import logging
from threading import Event, Thread

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import ExecutionJob, ExecutionJobStatus
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_workspace_id,
    reset_request_principal,
    set_request_identity,
)


class ExecutionWorkerMixin(RuntimeCapabilityHost):
    """Own execution worker operations."""

    def process_next_execution_job(self, worker_id: str) -> ExecutionJob | None:
        """Claim and process one durable job; return its terminal record."""
        self._ensure_bootstrapped()
        if not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        job = self.execution_jobs.claim_next_queued(
            worker_id.strip(), _now(), None, None
        )
        if job is None:
            return None
        identity_token = set_request_identity(
            job.requested_by_principal_id, job.org_id, job.workspace_id
        )
        cancellation_requested = Event()
        stop_heartbeat = Event()
        heartbeat_thread = Thread(
            target=self._renew_execution_job_lease,
            args=(job, cancellation_requested, stop_heartbeat),
            name=f"weaves-job-lease-{job.job_id[:12]}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            return self._complete_claimed_execution_job(
                job, cancellation_requested=cancellation_requested
            )
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)
            reset_request_principal(identity_token)

    def _renew_execution_job_lease(
        self,
        job: ExecutionJob,
        cancellation_requested: Event,
        stop_event: Event,
    ) -> None:
        if job.worker_id is None:
            return
        poll_seconds = min(1.0, float(self.EXECUTION_JOB_HEARTBEAT_SECONDS))
        heartbeat_elapsed = 0.0
        while not stop_event.wait(poll_seconds):
            try:
                latest = self.execution_jobs.get(job.job_id)
            except KeyError:
                return
            if latest.status is ExecutionJobStatus.CANCEL_REQUESTED:
                cancellation_requested.set()
            elif latest.status is not ExecutionJobStatus.RUNNING:
                return
            heartbeat_elapsed += poll_seconds
            if heartbeat_elapsed < self.EXECUTION_JOB_HEARTBEAT_SECONDS:
                continue
            heartbeat_elapsed = 0.0
            try:
                owned = self.heartbeat_execution_job(
                    job.job_id, worker_id=job.worker_id, attempts=job.attempts
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "execution job lease heartbeat failed job_id=%s", job.job_id
                )
                continue
            if not owned:
                self._handle_lost_execution_job_lease(job, cancellation_requested)
                return

    def _handle_lost_execution_job_lease(
        self, job: ExecutionJob, cancellation_requested: Event
    ) -> None:
        """Observe lease loss and propagate a pending cancellation request."""
        try:
            latest = self.execution_jobs.get(job.job_id)
        except KeyError:
            return
        if latest.status is ExecutionJobStatus.RUNNING:
            logging.getLogger(__name__).warning(
                "execution job lease lost job_id=%s worker_id=%s",
                job.job_id,
                job.worker_id,
            )
        elif latest.status is ExecutionJobStatus.CANCEL_REQUESTED:
            cancellation_requested.set()

    def _execution_cancellation_requested(
        self, job_id: str, cancellation_requested: Event
    ) -> bool:
        """Refresh cooperative cancellation from durable job state.

        The refresh happens at a safe boundary.
        """
        if cancellation_requested.is_set():
            return True
        try:
            latest = self.execution_jobs.get_scoped(
                job_id, effective_org_id(), effective_workspace_id()
            )
        except KeyError:
            return False
        if latest.status in {
            ExecutionJobStatus.CANCEL_REQUESTED,
            ExecutionJobStatus.CANCELLED,
        }:
            cancellation_requested.set()
        return cancellation_requested.is_set()
