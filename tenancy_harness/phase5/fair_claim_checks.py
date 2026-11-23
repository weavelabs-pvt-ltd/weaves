"""Slice 5.2 control T5-I02: a hot tenant cannot starve another.

Runs in the seed container after the two native workers (four slots each, an
enforced running cap of two per organization) are up. Organization A queues a
deep backlog of slow fake-model tasks; organization B then submits one task
whose model answers immediately. Everything goes through the real API and the
real workers; the database is only observed (owner connection, read only) and,
at the end, A's unstarted backlog is cancelled so later controls are not slowed.

The bound is declared before the run: B must start within ``START_BOUND``
seconds although every one of A's tasks takes ``SLOW_SECONDS``, so B cannot have
waited for any of A's work to finish.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import psycopg

from tenancy_harness.client import call
from tenancy_harness.resources import definitions, tenant

CONTROL_ID = "T5-I02"
SLICE = "5.2"

BACKLOG = 100  # A's queued tasks
SLOW_SECONDS = 20  # each of A's model calls
START_BOUND = 8.0  # B must start within this, well below SLOW_SECONDS
RUNNING_CAP = 2  # WEAVES_LIMIT_MAX_RUNNING_PER_ORG in the harness workers


def turns(org_id):
    """Status counts of an organization's task turns (read only)."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        rows = connection.execute(
            "SELECT payload->>'status', count(*) FROM product_records "
            "WHERE collection = 'task_turns' AND org_id = %s GROUP BY 1",
            (org_id,),
        ).fetchall()
    return {status: int(count) for status, count in rows}


def turn_of(task_id, org_id):
    """The first turn of one task, or None while it does not exist yet."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        row = connection.execute(
            "SELECT payload FROM product_records WHERE collection = 'task_turns' "
            "AND org_id = %s AND payload->>'task_id' = %s",
            (org_id, task_id),
        ).fetchone()
    return row[0] if row else None


def wait_until(predicate, timeout):
    """Poll quickly until ``predicate`` returns a truthy value or time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def seconds_between(first, second):
    """Elapsed seconds between two ISO timestamps from the stored payload."""
    parse = datetime.fromisoformat
    return (
        parse(second.replace("Z", "+00:00")) - parse(first.replace("Z", "+00:00"))
    ).total_seconds()


def cancel_unstarted(org_id):
    """Fixture cleanup: stop A's queued backlog so later controls are unaffected."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "UPDATE product_records SET payload = payload || "
            "jsonb_build_object('status', 'cancelled'), updated_at = now() "
            "WHERE collection = 'task_turns' AND org_id = %s "
            "AND payload->>'status' = 'queued'",
            (org_id,),
        )


def submit_backlog(a):
    """Queue ``BACKLOG`` slow tasks for the hot organization through the API."""
    for index in range(BACKLOG):
        call(
            "POST",
            "/tasks",
            a["admin"],
            {
                "agent_id": a["agent"]["id"],
                "title": f"fair claim backlog {index}",
                "initial_message": f"tenancy-slow={SLOW_SECONDS} backlog {index}",
            },
        )


def fair_claim_control():
    """Backlog A with slow work, submit one task for B, measure, return booleans."""
    a, b = tenant("fairclaim-a"), tenant("fairclaim-b")
    definitions(a)
    definitions(b)
    submit_backlog(a)
    # A fills exactly its running cap while the rest of its backlog waits.
    saturated = wait_until(
        lambda: turns(a["org_id"]).get("running", 0) >= RUNNING_CAP, 60
    )
    before = turns(a["org_id"])
    peak = before.get("running", 0)

    def sample():
        nonlocal peak
        peak = max(peak, turns(a["org_id"]).get("running", 0))

    submitted = time.monotonic()
    task = call(
        "POST",
        "/tasks",
        b["admin"],
        {
            "agent_id": b["agent"]["id"],
            "title": "fair claim tenant B",
            "initial_message": "tenant B needs one quick answer",
        },
    )
    started = wait_until(
        lambda: (
            sample() is None
            and (turn := turn_of(task["task_id"], b["org_id"]))
            and turn.get("status") in {"running", "succeeded", "failed"}
            and turn
        ),
        START_BOUND * 4,
    )
    waited = time.monotonic() - submitted
    finished = wait_until(
        lambda: (
            sample() is None
            and (turn := turn_of(task["task_id"], b["org_id"]))
            and turn.get("status") in {"succeeded", "failed"}
            and turn
        ),
        30,
    )
    # Sample A's running count for a while after B is done: it never exceeds the cap.
    for _ in range(30):
        sample()
        time.sleep(0.1)
    after = turns(a["org_id"])
    cancel_unstarted(a["org_id"])
    return {
        "backlog_is_queued": sum(before.values()) >= BACKLOG
        and before.get("queued", 0) >= BACKLOG - 10,
        "backlogged_org_runs_at_its_cap": bool(saturated)
        and before.get("running") == RUNNING_CAP,
        "other_org_starts_within_the_bound": bool(started) and waited <= START_BOUND,
        "other_org_waits_less_than_one_slow_call": bool(started)
        and waited < SLOW_SECONDS / 2,
        "other_org_finishes": bool(finished) and finished.get("status") == "succeeded",
        "backlogged_org_never_exceeds_its_cap": peak <= RUNNING_CAP
        and after.get("running", 0) <= RUNNING_CAP,
        "measured": {
            "waited_seconds": round(waited, 2),
            "bound_seconds": START_BOUND,
            "slow_call_seconds": SLOW_SECONDS,
            "peak_running_a": peak,
            "claimed_by": started.get("worker_id") if started else None,
        },
    }


def main():
    """Record the control in the private fixture file the browser suite reads."""
    from tenancy_harness.seed import require_disposable_database

    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    fixtures["fair_claim"] = fair_claim_control()
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Fair claim control recorded.")


if __name__ == "__main__":
    main()
