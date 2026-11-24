"""In-container helpers shared by the slice 5.6 load scenarios.

These run inside the harness ``seed`` container, on the isolated Compose network,
against the real API and the real native worker. Only the model provider is fake.
The database is read (and, for fixture cleanup and bulk history seeding, written)
through the disposable owner connection, exactly like the other Phase 5 controls.
"""

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg

from tenancy_harness.client import call, request
from tenancy_harness.phase5.load_stats import metric, parse_prometheus, summarize
from tenancy_harness.resources import tenant

OUTPUT = Path(os.environ.get("TENANCY_LOAD_OUTPUT", "/output"))
API_METRICS = "http://api:8000/api/v0/internal/metrics"
WORKER_METRICS = "http://worker:9100/metrics"
ACCEPTED = (200, 201, 202)  # POST /tasks answers 202 Accepted
ACTIVE = "('queued', 'running')"


def rows(query, *params):
    """Read (or run a statement) on the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection:
        cursor = connection.execute(query, params or None)
        return cursor.fetchall() if cursor.description else None


def write_result(name, data):
    """Persist one scenario result for the host orchestrator (owner-only file)."""
    path = OUTPUT / f"load-{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    path.chmod(0o600)
    return path


def now():
    """Timezone-aware wall clock, comparable with database timestamps."""
    return datetime.now(UTC)


def light_tenant(label):
    """A real organization with one model provider and one agent (no extras)."""
    t = tenant(label)
    provider = call(
        "POST",
        "/model-providers",
        t["admin"],
        {
            "provider_type": "openai_compatible",
            "display_name": t["marker"],
            "api_key": "fixture-model-" + label,
            "base_url": "https://models.tenancy.test/v1",
            "initial_model": "tenancy-fixture-model",
        },
    )
    agent = call(
        "POST",
        "/agents",
        t["admin"],
        {
            "name": "load-" + label,
            "instructions": t["marker"],
            "description": t["marker"],
            "allowed_capability_ids": [],
        },
    )
    t.update({"provider_id": provider["id"], "agent": agent})
    return t


def submit_task(t, message, key=None):
    """POST one task; never raises, so a load run records refusals and errors."""
    headers = dict(t["admin"]["headers"])
    if key:
        headers["Idempotency-Key"] = key
    body = {
        "agent_id": t["agent"]["id"],
        "title": message[:60],
        "initial_message": message,
    }
    started = time.monotonic()
    try:
        response = request("POST", "/tasks", headers, body)
    except httpx.HTTPError:
        return {"status": 0, "seconds": time.monotonic() - started, "task_id": None}
    task_id = None
    if response.status_code in ACCEPTED:
        task_id = response.json().get("task_id")
    return {
        "status": response.status_code,
        "seconds": time.monotonic() - started,
        "task_id": task_id,
    }


def depth(org_id, collection="task_turns"):
    """Queued plus running rows of an organization, counted from the rows."""
    return rows(
        "SELECT count(*) FROM product_records WHERE collection = %s AND "
        f"org_id = %s AND payload->>'status' IN {ACTIVE}",
        collection,
        org_id,
    )[0][0]


def turn_rows(org_id):
    """(created, started, finished, status, attempts) of every turn of an org."""
    return rows(
        "SELECT created_at, payload->>'started_at', payload->>'finished_at', "
        "payload->>'status', payload->>'task_id' FROM product_records "
        "WHERE collection = 'task_turns' AND org_id = %s",
        org_id,
    )


def cancel_queued(org_id):
    """Fixture cleanup: stop an organization's unstarted backlog."""
    rows(
        "UPDATE product_records SET payload = payload || "
        "jsonb_build_object('status', 'cancelled'), updated_at = now() "
        "WHERE collection = 'task_turns' AND org_id = %s "
        "AND payload->>'status' = 'queued'",
        org_id,
    )


def wait_idle(timeout):
    """Wait until no task turn anywhere is running or queued; True when idle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        busy = rows(
            "SELECT count(*) FROM product_records WHERE collection = 'task_turns' "
            f"AND payload->>'status' IN {ACTIVE}"
        )[0][0]
        if not busy:
            return True
        time.sleep(1)
    return False


def scrape(url):
    """Parse one internal metrics endpoint; empty when unreachable."""
    try:
        response = httpx.get(url, timeout=5, trust_env=False)
    except httpx.HTTPError:
        return {}
    return parse_prometheus(response.text) if response.status_code == 200 else {}


def connections():
    """(total, active) PostgreSQL connections of the application database."""
    total, active = rows(
        "SELECT count(*), count(*) FILTER (WHERE state = 'active') "
        "FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid()"
    )[0]
    return int(total), int(active)


class Sampler(threading.Thread):
    """Every few seconds: pool use, queue gauges and database connections."""

    def __init__(self, interval=5.0):
        """Create a stopped sampler."""
        super().__init__(daemon=True)
        self.interval, self.stop_event, self.samples = interval, threading.Event(), []

    def run(self):
        """Sample until ``stop`` is called."""
        while not self.stop_event.wait(self.interval):
            api, worker = scrape(API_METRICS), scrape(WORKER_METRICS)
            total, active = connections()
            self.samples.append(
                {
                    "api_pool_in_use": metric(api, "db_pool_in_use"),
                    "worker_pool_in_use": metric(worker, "db_pool_in_use"),
                    "queue_depth": metric(worker, "queue_depth"),
                    "oldest_queued_age_seconds": max(
                        (
                            v
                            for (name, _), v in worker.items()
                            if name == "oldest_queued_age_seconds"
                        ),
                        default=None,
                    ),
                    "running": metric(worker, "running"),
                    "db_connections": total,
                    "db_active": active,
                }
            )

    def stop(self):
        """Stop sampling and wait for the thread."""
        self.stop_event.set()
        self.join(timeout=10)

    def report(self):
        """Peak of every sampled series (None-safe)."""
        keys = self.samples[0].keys() if self.samples else ()
        peaks = {}
        for key in keys:
            values = [s[key] for s in self.samples if s[key] is not None]
            peaks[key + "_peak"] = max(values) if values else None
        return {"samples": len(self.samples), **peaks}


class Poller(threading.Thread):
    """A tenant's open browser tab: polls Home and the task list like the UI."""

    PATHS = ("/overview?tz_offset_minutes=0", "/tasks?limit=20")

    def __init__(self, actor, interval=1.5):
        """Poll the paths round robin every ``interval`` seconds until stopped."""
        super().__init__(daemon=True)
        self.actor, self.interval = actor, interval
        self.stop_event = threading.Event()
        self.latency = {path: [] for path in self.PATHS}
        self.statuses = {}

    def run(self):
        """Issue one request per tick, recording latency and status."""
        turn = 0
        while not self.stop_event.wait(self.interval):
            path = self.PATHS[turn % len(self.PATHS)]
            turn += 1
            started = time.monotonic()
            try:
                status = request("GET", path, self.actor["headers"]).status_code
            except httpx.HTTPError:
                status = 0
            self.latency[path].append(time.monotonic() - started)
            self.statuses[status] = self.statuses.get(status, 0) + 1

    def stop(self):
        """Stop polling and wait for the thread."""
        self.stop_event.set()
        self.join(timeout=10)

    def report(self):
        """Latency summary per path and the status histogram."""
        return {
            "latency_seconds": {
                path.split("?")[0]: summarize(values)
                for path, values in self.latency.items()
            },
            "statuses": self.statuses,
        }
