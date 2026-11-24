"""Slice 5.6 scenarios 2 and 4: tenant-count growth and very large histories.

Runs inside the harness ``seed`` container. It grows the number of organizations
in stages (each with its own agent and a little finished work), and at every
stage measures, with the system otherwise idle:

* the database work the idle worker and API generate (rows read, sequential
  scans of ``product_records``) per second, from ``pg_stat_database`` and
  ``pg_stat_user_tables`` deltas: recovery and polling cost must stay flat;
* how long a probe tenant's task waits for a slot (claim cost);
* how fast one task in every organization drains (fair claiming across N orgs);
* API latency percentiles of the key endpoints for the probe tenant.

Finally it gives one tenant a history of thousands of tasks (the SQL copy used by
the 5.4 control) and repeats the endpoint latency and idle-cost measurements, and
walks a full keyset pagination to prove the largest response stays one page.
"""

import argparse
import json
import threading
import time

import httpx

from tenancy_harness.client import request, wait_for_services
from tenancy_harness.phase5.load_common import (
    light_tenant,
    now,
    rows,
    submit_task,
    turn_rows,
    wait_idle,
    write_result,
)
from tenancy_harness.phase5.load_stats import parse_iso, summarize, turn_timings

PAGE = 50
ENDPOINTS = (
    "/overview?tz_offset_minutes=0",
    "/tasks?limit=50",
    "/tasks?paginate=true&limit=50",
    "/agents",
    "/users",
    "/usage",
)


def db_counters():
    """Cumulative work counters of the application database and its big table."""
    database = rows(
        "SELECT tup_returned, tup_fetched, xact_commit FROM pg_stat_database "
        "WHERE datname = current_database()"
    )[0]
    table = rows(
        "SELECT seq_scan, seq_tup_read, idx_scan, n_live_tup FROM "
        "pg_stat_user_tables WHERE relname = 'product_records'"
    )[0]
    return [int(v) for v in (*database, *table)]


def idle_cost(seconds):
    """Rows read and sequential scans per second while nothing is submitted."""
    first = db_counters()
    time.sleep(seconds)
    last = db_counters()
    delta = [b - a for a, b in zip(first, last, strict=True)]
    return {
        "seconds": seconds,
        "tup_returned_per_s": round(delta[0] / seconds, 1),
        "tup_fetched_per_s": round(delta[1] / seconds, 1),
        "commits_per_s": round(delta[2] / seconds, 2),
        "seq_scans_of_records_per_s": round(delta[3] / seconds, 3),
        "seq_tuples_read_per_s": round(delta[4] / seconds, 1),
        "records_live_tuples": last[6],
    }


def finish(task_ids, org_id, timeout=120):
    """Wait for the given tasks' turns to reach a terminal state; return them."""
    deadline = time.monotonic() + timeout
    wanted = set(task_ids)
    while time.monotonic() < deadline:
        done = {
            row for row in turn_rows(org_id) if row[4] in wanted and row[3] != "queued"
        }
        if len({row[4] for row in done if row[3] in {"succeeded", "failed"}}) == len(
            wanted
        ):
            return list(done)
        time.sleep(0.5)
    return []


def claim_probe(t, count=5):
    """Submit ``count`` fast tasks one at a time to an idle system; delays in s."""
    delays = []
    for index in range(count):
        sent = submit_task(t, f"scale probe {index}")
        done = finish([sent["task_id"]], t["org_id"], 60) if sent["task_id"] else []
        for created, started, finished, _status, _task in done:
            timing = turn_timings(
                created,
                parse_iso(started) if started else None,
                parse_iso(finished) if finished else None,
            )
            if timing["queue_delay"] is not None:
                delays.append(timing["queue_delay"])
        time.sleep(0.5)
    return summarize(delays)


def endpoint_latency(t, repeat=20):
    """Latency percentiles and statuses of the key endpoints for one tenant."""
    result = {}
    for path in ENDPOINTS:
        seconds, statuses = [], {}
        for _ in range(repeat):
            started = time.monotonic()
            try:
                status = request("GET", path, t["admin"]["headers"]).status_code
            except httpx.HTTPError:
                status = 0
            seconds.append(time.monotonic() - started)
            time.sleep(0.3)  # a person's pace, under the per-principal limit
            statuses[status] = statuses.get(status, 0) + 1
        result[path] = {"seconds": summarize(seconds), "statuses": statuses}
    return result


def drain_all(tenants):
    """One fast task in every organization; seconds to drain and tasks/minute."""
    started = time.monotonic()
    sent = [(t, submit_task(t, "scale drain")) for t in tenants]
    accepted = [(t, s["task_id"]) for t, s in sent if s["task_id"]]
    wait_idle(600)
    seconds = time.monotonic() - started
    return {
        "organizations": len(tenants),
        "accepted": len(accepted),
        "seconds": round(seconds, 1),
        "tasks_per_minute": round(len(accepted) * 60 / seconds, 1),
    }


def grow(tenants, target, label):
    """Create organizations until ``target`` exist, each with finished work."""
    while len(tenants) < target:
        t = light_tenant(f"{label}{len(tenants)}")
        submit_task(t, "scale seed")
        tenants.append(t)
    wait_idle(600)


def stage(tenants, target, args):
    """Grow to ``target`` organizations and take every measurement."""
    started = time.monotonic()
    grow(tenants, target, args.label)
    created_seconds = round(time.monotonic() - started, 1)
    return {
        "organizations": len(tenants),
        "created_in_seconds": created_seconds,
        "idle_cost": idle_cost(args.idle_seconds),
        "claim_delay_seconds": claim_probe(tenants[0]),
        "drain": drain_all(tenants),
        "api": endpoint_latency(tenants[0]),
    }


def seed_history(big, template_org, count):
    """Copy a finished task and turn ``count`` times into ``big``'s workspace."""
    task_id, turn_id = rows(
        "SELECT t.record_id, t.payload->>'latest_turn_id' FROM product_records t "
        "WHERE t.collection = 'tasks' AND t.org_id = %s "
        "AND t.payload->>'latest_turn_status' = 'succeeded' LIMIT 1",
        template_org,
    )[0]
    org, workspace = big["org_id"], big["workspace_id"]
    principal = big["admin"]["principal_id"]
    own = json.dumps(
        {
            "org_id": org,
            "workspace_id": workspace,
            "created_by_principal_id": principal,
            "requested_by_principal_id": principal,
            "audience": "workspace",
            "audience_owner_principal_id": None,
            "private_skill_snapshots": [],
            "idempotency_key": None,
        }
    )
    stamps = (
        "SELECT n, now() - make_interval(mins => n * 5) AS ts "
        "FROM generate_series(1, %s) n"
    )
    for collection, source, extra in (
        (
            "tasks",
            task_id,
            "jsonb_build_object('task_id', 'ld-task-'||n, "
            "'latest_turn_id', 'ld-turn-'||n, 'title', 'History '||n,",
        ),
        (
            "task_turns",
            turn_id,
            "jsonb_build_object('turn_id', 'ld-turn-'||n, 'task_id', 'ld-task-'||n, "
            "'run_idempotency_key', null, 'status', 'succeeded',",
        ),
    ):
        rows(
            "INSERT INTO product_records (collection, record_id, org_id, "
            "workspace_id, requested_by_principal_id, payload, created_at, "
            "updated_at) SELECT %s, 'ld-"
            + ("task-" if collection == "tasks" else "turn-")
            + "'||n, %s, %s, %s, payload || %s::jsonb || "
            + extra
            + " 'created_at', to_jsonb(ts), 'updated_at', to_jsonb(ts)), ts, ts "
            "FROM product_records t, LATERAL ("
            + stamps
            + ") g WHERE t.collection = %s AND t.record_id = %s",
            collection,
            org,
            workspace,
            principal,
            own,
            count,
            collection,
            source,
        )
    rows("ANALYZE product_records")


def walk_pages(t, limit=PAGE):
    """Follow ``next_cursor`` to the end: (tasks seen, pages, largest page, 429s).

    A refusal by the request limiter (the walk is faster than any person) is
    waited out as ``Retry-After`` says, and counted.
    """
    seen, pages, largest, cursor, refused = 0, 0, 0, "", 0
    for _ in range(10_000):
        suffix = f"&cursor={cursor}" if cursor else ""
        response = request(
            "GET",
            f"/tasks?paginate=true&limit={limit}{suffix}",
            t["admin"]["headers"],
        )
        if response.status_code == 429:
            refused += 1
            time.sleep(min(float(response.headers.get("retry-after", "1")), 10))
            continue
        if response.status_code != 200:
            raise AssertionError(f"/tasks page: HTTP {response.status_code}")
        body = response.json()
        pages, seen = pages + 1, seen + len(body["items"])
        largest = max(largest, len(body["items"]))
        cursor = body["next_cursor"] or ""
        if not cursor:
            return seen, pages, largest, refused
    raise AssertionError("cursor walk did not terminate")


def history_stage(tenants, args):
    """Give one tenant a huge history; measure its endpoints and the idle cost."""
    big = light_tenant(args.label + "big")
    seed_history(big, tenants[0]["org_id"], args.history)
    total = rows(
        "SELECT count(*) FROM product_records WHERE collection = 'tasks' "
        "AND org_id = %s",
        big["org_id"],
    )[0][0]
    started = time.monotonic()
    seen, pages, largest, refused = walk_pages(big)
    return {
        "tasks_in_history": total,
        "keyset_walk": {
            "tasks_seen": seen,
            "pages": pages,
            "largest_page": largest,
            "rate_limited_retries": refused,
            "seconds": round(time.monotonic() - started, 1),
        },
        "idle_cost": idle_cost(args.idle_seconds),
        "claim_delay_seconds": claim_probe(big),
        "api": endpoint_latency(big),
        "other_tenant_api": endpoint_latency(tenants[0]),
    }


def api_capacity(tenants, threads, seconds):
    """Closed-loop load on Home polling across many tenants: the API's ceiling.

    Each thread cycles through the tenants (so no tenant nears its own request
    limits) calling the two requests an open tab repeats. The achieved rate is
    the single API process's throughput on the pinned CPUs, shared with the
    database; latency is what a poller sees at that rate.
    """
    end = time.monotonic() + seconds
    lock = threading.Lock()
    seconds_list, statuses = [], {}

    def worker(offset):
        turn, mine, seen = offset, [], {}
        while time.monotonic() < end:
            actor = tenants[turn % len(tenants)]["admin"]["headers"]
            path = ("/overview?tz_offset_minutes=0", "/tasks?limit=20")[turn % 2]
            turn += threads
            started = time.monotonic()
            try:
                status = request("GET", path, actor).status_code
            except httpx.HTTPError:
                status = 0
            mine.append(time.monotonic() - started)
            seen[status] = seen.get(status, 0) + 1
        with lock:
            seconds_list.extend(mine)
            for status, count in seen.items():
                statuses[status] = statuses.get(status, 0) + count

    pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    started = time.monotonic()
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    elapsed = time.monotonic() - started
    return {
        "threads": threads,
        "organizations": len(tenants),
        "requests_per_second": round(len(seconds_list) / elapsed, 1),
        "latency_seconds": summarize(seconds_list),
        "statuses": statuses,
    }


def run(args):
    """All stages; the result is written for the orchestrator."""
    tenants = []
    stages = [stage(tenants, n, args) for n in args.stages]
    write_result(args.label, {"config": vars(args), "stages": stages})
    result = {
        "config": vars(args),
        "started_at": now(),
        "stages": stages,
        "history": history_stage(tenants, args),
        "api_capacity": [
            api_capacity(tenants, threads, args.capacity_seconds)
            for threads in (2, 4, 8, 16)
        ],
    }
    write_result(args.label, result)
    return result


def main(argv=None):
    """Run the growth scenario and print the stage summary."""
    from tenancy_harness.seed import require_disposable_database

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", required=True)
    p.add_argument("--stages", type=lambda s: [int(x) for x in s.split(",")])
    p.add_argument("--history", type=int, default=6000)
    p.add_argument("--capacity-seconds", type=int, default=30, dest="capacity_seconds")
    p.add_argument("--idle-seconds", type=int, default=60, dest="idle_seconds")
    args = p.parse_args(argv)
    args.stages = args.stages or [10, 50, 100, 200]
    require_disposable_database()
    wait_for_services()
    result = run(args)
    for item in result["stages"]:
        print(
            "orgs",
            item["organizations"],
            "claim p95",
            item["claim_delay_seconds"]["p95"],
            "seq tuples/s",
            item["idle_cost"]["seq_tuples_read_per_s"],
        )


if __name__ == "__main__":
    main()
