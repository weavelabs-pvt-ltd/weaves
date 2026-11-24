"""Optional internal metrics listener for the worker process.

The worker has no HTTP API, so its counters (model calls, tokens, queue gauges)
would otherwise be invisible. Set ``WEAVES_WORKER_METRICS_PORT`` to expose them
as Prometheus text on ``/metrics``. The port is for the private Docker network;
deployment files must never publish it.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from shared.metrics import REGISTRY

logger = logging.getLogger("product.worker")
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        """Serve the registry on ``/metrics`` and refuse every other path."""
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = REGISTRY.render().encode()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Scrapes are not logged."""


def start_metrics_server(port: int) -> ThreadingHTTPServer:
    """Serve metrics on ``port`` from a daemon thread; the caller shuts it down."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    Thread(
        target=server.serve_forever, name="weaves-worker-metrics", daemon=True
    ).start()
    logger.info("worker metrics listening", extra={"event": "metrics_listen"})
    return server
