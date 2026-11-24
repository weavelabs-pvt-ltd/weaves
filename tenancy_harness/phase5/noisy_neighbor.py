"""Slice 5.6 scenario 1: a saturated tenant A next to a well-behaved tenant B.

Runs inside the harness ``seed`` container against the real API and the real
native worker. Tenant A keeps its backlog at its queued cap with slow fake-model
calls (``tenancy-slow=N``); tenant B submits normal work at a steady rate. Every
number comes from the database rows the product itself wrote (created, started
and finished timestamps of each task turn), the API's internal metrics, and the
HTTP latency the load generator observed. The host orchestrator adds container
CPU and memory from ``docker stats``.

The service envelope for tenant B is declared in ``ENVELOPE`` and was committed
before the first measured run; ``verdict`` compares each run to it. The bounds
are properties of the product, not of one worker setting, so a setting that
misses them is reported as failing rather than the bounds being moved.
"""

import argparse
import threading
import time

from tenancy_harness.client import wait_for_services
from tenancy_harness.phase5.load_common import (
    ACCEPTED,
    Poller,
    Sampler,
    cancel_queued,
    depth,
    light_tenant,
    now,
    submit_task,
    turn_rows,
    wait_idle,
    write_result,
)
from tenancy_harness.phase5.load_stats import (
    parse_iso,
    summarize,
    throughput_per_minute,
    turn_timings,
    within,
)

# Declared before the run (see the module docstring). Seconds unless stated.
ENVELOPE = {
    "b_queue_delay_p95": 15.0,
    "b_queue_delay_max": 45.0,
    "b_completion_p95": 30.0,
    "b_failed_turns": 0,
    "b_api_p95": 1.0,
    "b_http_errors": 0,
}


def feed_a(stop, a, config, log):
    """Keep A's backlog near ``backlog`` with slow work; count refusals."""
    index = 0
    while not stop.wait(1.0):
        for _ in range(max(0, config["backlog"] - depth(a["org_id"]))):
            index += 1
            result = submit_task(
                a, f"tenancy-slow={config['a_slow']} hot tenant {index}"
            )
            log.append(result)
            if result["status"] not in ACCEPTED:
                break


def feed_b(stop, b, config, log):
    """Submit B's normal tasks at a fixed rate (open loop, no catch-up bursts)."""
    index, next_at = 0, time.monotonic()
    while not stop.is_set():
        index += 1
        log.append(
            submit_task(b, f"tenancy-slow={config['b_slow']} normal work {index}")
        )
        next_at += config["b_interval"]
        if stop.wait(max(0.0, next_at - time.monotonic())):
            return


def org_summary(org_id, window):
    """Queue delay, completion latency and outcomes of one org's measured turns."""
    start, end = window
    records = turn_rows(org_id)
    delays, completions, finished, statuses = [], [], [], {}
    for created, started, done, status, _task in records:
        if not start <= created <= end:
            continue
        statuses[status] = statuses.get(status, 0) + 1
        timing = turn_timings(
            created,
            parse_iso(started) if started else None,
            parse_iso(done) if done else None,
        )
        if timing["queue_delay"] is not None:
            delays.append(timing["queue_delay"])
        if timing["completion"] is not None and status == "succeeded":
            completions.append(timing["completion"])
    for _created, _started, done, status, _task in records:
        if done and status == "succeeded" and start <= parse_iso(done) <= end:
            finished.append(parse_iso(done).timestamp())
    seconds = (end - start).total_seconds()
    return {
        "turns": sum(statuses.values()),
        "statuses": statuses,
        "queue_delay_seconds": summarize(delays),
        "completion_seconds": summarize(completions),
        "completed_in_window": len(finished),
        "throughput_per_minute": throughput_per_minute(finished, seconds),
    }


def http_summary(log):
    """Submission outcomes from the load generator's point of view."""
    statuses = {}
    for item in log:
        status = item["status"] if isinstance(item, dict) else item
        statuses[status] = statuses.get(status, 0) + 1
    latencies = [
        i["seconds"] for i in log if isinstance(i, dict) and i["status"] in ACCEPTED
    ]
    return {"statuses": statuses, "post_latency_seconds": summarize(latencies)}


def verdict(b, poll, http):
    """Compare B's measured numbers with the declared envelope."""
    delay = b["queue_delay_seconds"]
    errors = sum(n for s, n in http["statuses"].items() if s not in ACCEPTED)
    api = [v["p95"] for v in poll["latency_seconds"].values() if v["p95"] is not None]
    checks = {
        "b_queue_delay_p95": within(delay["p95"], ENVELOPE["b_queue_delay_p95"]),
        "b_queue_delay_max": within(delay["max"], ENVELOPE["b_queue_delay_max"]),
        "b_completion_p95": within(
            b["completion_seconds"]["p95"], ENVELOPE["b_completion_p95"]
        ),
        "b_failed_turns": b["statuses"].get("failed", 0) <= ENVELOPE["b_failed_turns"],
        "b_api_p95": bool(api) and max(api) <= ENVELOPE["b_api_p95"],
        "b_http_errors": errors <= ENVELOPE["b_http_errors"],
    }
    return {"checks": checks, "within_envelope": all(checks.values())}


def run(config):
    """One measured run; returns the result document (also written to disk)."""
    a = light_tenant(config["label"] + "a")
    b = light_tenant(config["label"] + "b")
    stop, log_a, log_b = threading.Event(), [], []
    workers = [
        threading.Thread(target=feed_a, args=(stop, a, config, log_a), daemon=True),
        threading.Thread(target=feed_b, args=(stop, b, config, log_b), daemon=True),
    ]
    sampler, polls = Sampler(), {"a": Poller(a["admin"]), "b": Poller(b["admin"])}
    sampler.start()
    started = now()
    for worker in (*workers, *polls.values()):
        worker.start()
    time.sleep(config["warmup"])
    window_start = now()
    time.sleep(config["duration"])
    window_end = now()
    stop.set()
    for worker in workers:
        worker.join(timeout=30)
    # B's last tasks still get to finish; then A's unstarted backlog is dropped.
    deadline = time.monotonic() + 120
    while depth(b["org_id"]) and time.monotonic() < deadline:
        time.sleep(1)
    cancel_queued(a["org_id"])
    drained = wait_idle(config["a_slow"] + 60)
    for poller in polls.values():
        poller.stop()
    sampler.stop()
    window = (window_start, window_end)
    result = {
        "config": config,
        "started_at": started,
        "window_seconds": (window_end - window_start).total_seconds(),
        "a": org_summary(a["org_id"], window),
        "b": org_summary(b["org_id"], window),
        "a_http": http_summary(log_a),
        "b_http": http_summary(log_b),
        "pollers": {k: p.report() for k, p in polls.items()},
        "sampler": sampler.report(),
        "drained": drained,
    }
    result["verdict"] = verdict(result["b"], result["pollers"]["b"], result["b_http"])
    write_result(config["label"], result)
    return result


def parser():
    """Command line for one run; the orchestrator supplies every value."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", required=True)
    p.add_argument("--duration", type=int, default=300, help="measured seconds")
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--a-slow", type=int, default=25, dest="a_slow")
    p.add_argument("--b-slow", type=int, default=5, dest="b_slow")
    p.add_argument("--b-interval", type=float, default=20.0, dest="b_interval")
    p.add_argument("--backlog", type=int, default=45)
    return p


def main(argv=None):
    """Run one noisy-neighbour measurement and print its verdict."""
    from tenancy_harness.seed import require_disposable_database

    require_disposable_database()
    wait_for_services()
    result = run(vars(parser().parse_args(argv)))
    print(
        "noisy neighbour",
        result["config"]["label"],
        "b queue delay",
        result["b"]["queue_delay_seconds"],
        "within envelope:",
        result["verdict"]["within_envelope"],
    )


if __name__ == "__main__":
    main()
