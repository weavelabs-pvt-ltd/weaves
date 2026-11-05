"""Poll the durable product execution queue using the configured runtime."""

import logging
import os
import socket
import time
from uuid import uuid4

from apps.worker.services import WorkerServices
from apps.worker.wiring import build_worker_services

logger = logging.getLogger("product.worker")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _recover_worker_state(services: WorkerServices) -> None:
    """Recover stale task turns, action calls, and expired memory."""
    recovered_task_turns = services.tasks.recover_stale_turns()
    if recovered_task_turns:
        logger.warning("recovered stale task turns count=%d", len(recovered_task_turns))
    recovery = services.execution_recovery
    recovery.recover_stale_runs()
    recovered_jobs = recovery.recover_stale_execution_jobs()
    recovered_actions = services.action_recovery.recover_stale_action_invocations()
    if recovered_jobs:
        logger.warning("recovered stale execution jobs count=%d", len(recovered_jobs))
    if recovered_actions:
        logger.warning(
            "recovered stale action invocations count=%d", len(recovered_actions)
        )
    expired_memory = services.memory.purge_expired_memory_items()
    if expired_memory:
        logger.info("purged expired memory items count=%d", len(expired_memory))


def main() -> None:
    interval = float(os.environ.get("WEAVES_WORKER_POLL_SECONDS", "1"))
    if not 0.05 <= interval <= 60:
        raise ValueError("WEAVES_WORKER_POLL_SECONDS must be between 0.05 and 60")
    schedule_interval = float(os.environ.get("WEAVES_SCHEDULE_POLL_SECONDS", "5"))
    if not 0.5 <= schedule_interval <= 60:
        raise ValueError("WEAVES_SCHEDULE_POLL_SECONDS must be between 0.5 and 60")
    worker_id = os.environ.get("WEAVES_WORKER_ID", "").strip()
    worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:12]}"
    services = build_worker_services()
    try:
        if services.storage_mode != "postgres":
            raise RuntimeError("The execution worker requires DATABASE_URL")
        services.bootstrap()
        logger.info("product execution worker started worker_id=%s", worker_id)
        last_recovery = time.monotonic()
        last_schedule_poll = 0.0
        while True:
            try:
                if time.monotonic() - last_recovery >= 30:
                    _recover_worker_state(services)
                    last_recovery = time.monotonic()
                if time.monotonic() - last_schedule_poll >= schedule_interval:
                    scheduled_jobs = (
                        services.workflow_automation.dispatch_due_workflow_schedules()
                    )
                    if scheduled_jobs:
                        logger.info(
                            "workflow schedules dispatched count=%d",
                            len(scheduled_jobs),
                        )
                    last_schedule_poll = time.monotonic()
                if services.studio is not None:
                    services.studio.tick()
                job = services.execution.process_next_execution_job(worker_id)
                turn = services.tasks.process_next_turn(worker_id)
                if turn is not None:
                    logger.info(
                        "task turn finished turn_id=%s status=%s",
                        turn.turn_id,
                        turn.status.value,
                    )
                if job is None and turn is None:
                    time.sleep(interval)
                    continue
                if job is not None:
                    logger.info(
                        "product execution job finished job_id=%s status=%s run_id=%s",
                        job.job_id,
                        job.status.value,
                        job.run_id,
                    )
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("product execution worker iteration failed")
                time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("product execution worker stopped worker_id=%s", worker_id)
    finally:
        services.close()


if __name__ == "__main__":
    configure_logging()
    main()
