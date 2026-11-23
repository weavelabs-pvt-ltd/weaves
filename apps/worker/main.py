"""Poll the durable product execution queue using the configured runtime."""

import logging
import os
import signal
import socket
from threading import Event
from uuid import uuid4

from application.ports.limits import limit_policy_from_env
from apps.worker.loop import WorkerLoop
from apps.worker.ticks import build_tick_registry
from apps.worker.wiring import build_worker_services

logger = logging.getLogger("product.worker")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    interval = float(os.environ.get("WEAVES_WORKER_POLL_SECONDS", "1"))
    if not 0.05 <= interval <= 60:
        raise ValueError("WEAVES_WORKER_POLL_SECONDS must be between 0.05 and 60")
    schedule_interval = float(os.environ.get("WEAVES_SCHEDULE_POLL_SECONDS", "5"))
    if not 0.5 <= schedule_interval <= 60:
        raise ValueError("WEAVES_SCHEDULE_POLL_SECONDS must be between 0.5 and 60")
    lifecycle_interval = float(os.environ.get("WEAVES_LIFECYCLE_POLL_SECONDS", "30"))
    if not 0.5 <= lifecycle_interval <= 3600:
        raise ValueError("WEAVES_LIFECYCLE_POLL_SECONDS must be between 0.5 and 3600")
    limits = limit_policy_from_env(os.environ)
    worker_id = os.environ.get("WEAVES_WORKER_ID", "").strip()
    worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:12]}"
    stopping = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    services = build_worker_services()
    try:
        if services.storage_mode != "postgres":
            raise RuntimeError("The execution worker requires DATABASE_URL")
        services.bootstrap()
        logger.info(
            "product execution worker started worker_id=%s limits_mode=%s",
            worker_id,
            limits.config.mode,
        )
        registry = build_tick_registry(
            schedule_interval=schedule_interval, lifecycle_interval=lifecycle_interval
        )
        WorkerLoop(
            services,
            registry,
            worker_id=worker_id,
            poll_seconds=interval,
            stopping=stopping,
        ).run()
    except KeyboardInterrupt:
        logger.info("product execution worker stopped worker_id=%s", worker_id)
    finally:
        services.close()


if __name__ == "__main__":
    configure_logging()
    main()
