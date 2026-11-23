"""Poll the durable product execution queue using the configured runtime."""

import logging
import os
import signal
import socket
from http.server import ThreadingHTTPServer
from threading import Event
from uuid import uuid4

from application.ports.limits import limit_policy_from_env
from apps.worker.loop import WorkerLoop
from apps.worker.metrics_server import start_metrics_server
from apps.worker.ticks import build_tick_registry
from apps.worker.wiring import build_worker_services
from shared.logging_setup import configure_logging as configure_json_logging
from infrastructure.config.settings import load_pool_settings

logger = logging.getLogger("product.worker")


def configure_logging() -> None:
    if os.environ.get("WEAVES_LOG_FORMAT", "json").strip().lower() == "text":
        logging.basicConfig(
            level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        return
    configure_json_logging(
        "worker", level=os.environ.get("LOG_LEVEL", "INFO"), replace_root=True
    )


def effective_slots(configured: int, pool_max: int) -> int:
    """Slots the database pool can serve: each needs a connection plus headroom.

    A pool smaller than ``slots + 2`` would let executions starve each other and
    the supervisor, so the slot count is reduced (and logged) instead.
    """
    return max(1, min(configured, pool_max - 2))


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
    config = limits.config
    slots = effective_slots(
        config.worker_slots, load_pool_settings(process="worker").db_pool_max_size
    )
    if slots != config.worker_slots:
        logger.warning(
            "worker slots reduced to fit the database pool configured=%d effective=%d",
            config.worker_slots,
            slots,
        )
    services = build_worker_services(config.queue_age_warn_seconds)
    metrics_server: ThreadingHTTPServer | None = None
    try:
        if services.storage_mode != "postgres":
            raise RuntimeError("The execution worker requires DATABASE_URL")
        services.bootstrap()
        metrics_port = int(os.environ.get("WEAVES_WORKER_METRICS_PORT", "0") or 0)
        if metrics_port:
            metrics_server = start_metrics_server(metrics_port)
        logger.info(
            "product execution worker started worker_id=%s limits_mode=%s "
            "slots=%d fair_claim=%s",
            worker_id,
            config.mode,
            slots,
            config.fair_claim,
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
            limits=limits if config.fair_claim else None,
            slots=slots,
        ).run()
    except KeyboardInterrupt:
        logger.info("product execution worker stopped worker_id=%s", worker_id)
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
        services.close()


if __name__ == "__main__":
    configure_logging()
    main()
