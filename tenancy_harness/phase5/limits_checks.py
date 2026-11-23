"""Slice 5.1 control T5-I01: the API refuses the (cap+1)th task atomically.

Real stack, two phases driven by ``run.py`` (the host pauses the native worker
between nothing and ``burst`` so the queue cannot drain mid-burst, then lets it
run for ``settle``):

``burst`` (worker paused): a fresh organization gets a queue cap of 3 from the
operator script ``organization_limits.py`` (the override path, not an
environment edit), then twelve simultaneous task creations race at that cap.
Exactly three are admitted; the other nine get HTTP 429 ``limit_exceeded`` with
``Retry-After`` and only the caller's own numbers. A second organization is
unaffected while the first is full, replays of an admitted key still succeed at
the limit, and the same idempotency key never collides across tenants.

``settle`` (worker running): the admitted tasks finish, the provider saw exactly
one model call per admitted task and none for any refusal, finished work frees
capacity (the next creation is admitted and also completes), and the usage view
reports the same numbers.

Nothing here uses a testing endpoint: it signs in through the fake Google
provider, calls the public API and reads durable rows with the owner connection.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import psycopg

from tenancy_harness.client import PROVIDER, request
from tenancy_harness.resources import definitions, tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T5-I01"
SLICE = "5.1"

SCRIPT = Path("/suite/operator/organization_limits.py")
OP = ["--operator", "harness@tenancy.test", "--reason", "T5-I01 control"]
CAP = 3
ATTEMPTS = 12
SHARED_KEY = "t5i01-shared-key"


def operator(*argv):
    """Run the real operator script; return (exit status, JSON stdout)."""
    spec = importlib.util.spec_from_file_location("organization_limits", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = module.run(list(argv))
    text = out.getvalue().strip()
    return status, json.loads(text) if text else None


def rows(query, *params):
    """Read durable rows on the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(query, params).fetchall()


def count(org_id, collection, status=None):
    """Rows of one collection an organization owns (optionally one status)."""
    query = "SELECT count(*) FROM product_records WHERE org_id = %s AND collection = %s"
    if status is None:
        return rows(query, org_id, collection)[0][0]
    return rows(query + " AND payload->>'status' = %s", org_id, collection, status)[0][
        0
    ]


def model_calls(label):
    """Chat completions the fake provider received with this tenant's credential."""
    digest = hashlib.sha256(("Bearer fixture-model-" + label).encode()).hexdigest()
    events = httpx.get(PROVIDER + "/model-events", timeout=10).json()["events"]
    return sum(
        1
        for event in events
        if event["path"] == "/v1/chat/completions"
        and event["credential_sha256"] == digest
    )


def create_task(t, name, key=None):
    """POST one task and return the raw response (never raising on 429)."""
    headers = dict(t["admin"]["headers"])
    if key:
        headers["Idempotency-Key"] = key
    body = {
        "agent_id": t["agent"]["id"],
        "title": name,
        "initial_message": name,
    }
    return request("POST", "/tasks", headers, body)


def limit_view(t):
    """The organization's own limits view, or None when it is not readable yet."""
    response = request("GET", "/organization/limits", t["admin"]["headers"])
    if response.status_code != 200:
        return None
    return {item["resource"]: item for item in response.json()["limits"]}


def wait_for_cap(t, seconds=60):
    """Wait for the API's brief limit cache to pick up the operator override."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        view = limit_view(t)
        if view and view["queued_work"]["limit"] == CAP:
            return True
        time.sleep(1)
    return False


def wait_for_tasks(t, expected, seconds=120):
    """Wait until every task of the organization finished in the real worker."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        done = rows(
            "SELECT count(*) FROM product_records WHERE org_id = %s "
            "AND collection = 'task_turns' AND payload->>'status' = 'succeeded'",
            t["org_id"],
        )[0][0]
        if done >= expected:
            return True
        time.sleep(0.5)
    return False


def race_at_the_cap(limited):
    """Twelve simultaneous creations at the cap; return every response."""
    gate = Barrier(ATTEMPTS)

    def attempt(index):
        gate.wait()
        return create_task(limited, f"t5i01 race {index}", f"t5i01-race-{index}")

    with ThreadPoolExecutor(max_workers=ATTEMPTS) as pool:
        return list(pool.map(attempt, range(ATTEMPTS)))


def refusal_results(limited, responses):
    """Exactly the cap admitted; the rest refused with the documented contract."""
    admitted = [r for r in responses if r.status_code == 202]
    refused = [r for r in responses if r.status_code == 429]
    expected = {
        "detail": "limit reached for queued_work",
        "code": "limit_exceeded",
        "resource": "queued_work",
        "limit": CAP,
        "used": CAP,
    }
    org = limited["org_id"]
    return {
        "exactly_the_cap_is_admitted": len(admitted) == CAP
        and len(refused) == ATTEMPTS - CAP,
        "refusals_use_the_limit_exceeded_contract": bool(refused)
        and all(r.json() == expected for r in refused)
        and all(int(r.headers.get("Retry-After", "0")) >= 1 for r in refused)
        and all(org not in r.text for r in refused),
        "queued_rows_never_exceed_the_cap": count(org, "task_turns", "queued") == CAP,
        "refusals_create_no_rows": count(org, "tasks") == CAP
        and count(org, "task_turns") == CAP
        and count(org, "agent_runs") == 0,
    }


def isolation_results(limited, other, responses):
    """Other tenants, shared keys and replays at the limit."""
    in_other = create_task(other, "t5i01 other org", SHARED_KEY)
    in_limited = create_task(limited, "t5i01 other org", SHARED_KEY)
    replay_other = create_task(other, "t5i01 other org", SHARED_KEY)
    replays = {
        i: create_task(limited, f"t5i01 race {i}", f"t5i01-race-{i}")
        for i in range(ATTEMPTS)
        if responses[i].status_code == 202
    }
    return {
        "other_organization_is_unaffected": in_other.status_code == 202
        and count(other["org_id"], "tasks") == 1,
        "same_key_in_another_tenant_is_a_separate_admission": (
            in_limited.status_code == 429
            and replay_other.status_code in {200, 202}
            and replay_other.json()["task_id"] == in_other.json()["task_id"]
        ),
        "replays_at_the_limit_return_the_existing_task": len(replays) == CAP
        and all(
            replay.status_code in {200, 202}
            and replay.json()["task_id"] == responses[i].json()["task_id"]
            for i, replay in replays.items()
        )
        and count(limited["org_id"], "tasks") == CAP,
    }


def burst_phase():
    """With the worker paused: set the cap, race at it, check refusals."""
    limited, other = tenant("p5l"), tenant("p5m")
    for t in (limited, other):
        definitions(t)
    status, shown = operator(
        "set", "--organization-id", limited["org_id"], *OP, "--max-queued", str(CAP)
    )
    cap_visible = (
        status == 0
        and shown["effective"]["max_queued_per_org"] == CAP
        and wait_for_cap(limited)
    )
    responses = race_at_the_cap(limited)
    results = {
        "operator_override_sets_the_cap": cap_visible,
        **refusal_results(limited, responses),
        **isolation_results(limited, other, responses),
        "no_model_call_before_the_worker_runs": model_calls("p5l") == 0,
    }
    return {"tenants": {"limited": limited, "other": other}, "results": results}


def settle_phase(state):
    """With the worker running: admitted work completes, refusals cost nothing."""
    limited, other = state["tenants"]["limited"], state["tenants"]["other"]
    drained = wait_for_tasks(limited, CAP) and wait_for_tasks(other, 1)
    calls_after_drain = model_calls("p5l")
    freed = create_task(limited, "t5i01 after drain", "t5i01-after-drain")
    finished_again = freed.status_code == 202 and wait_for_tasks(limited, CAP + 1)
    view = limit_view(limited) or {}
    return {
        **state["results"],
        "admitted_work_completes_in_the_worker": drained,
        "one_model_call_per_admitted_task": calls_after_drain == CAP
        and model_calls("p5m") == 1,
        "finished_work_frees_capacity": finished_again
        and model_calls("p5l") == CAP + 1,
        "usage_view_matches_the_rows": view.get("queued_work", {}).get("limit") == CAP
        and view["queued_work"]["used"]
        == count(limited["org_id"], "task_turns", "queued")
        and view["tasks"]["used"] == CAP + 1,
    }


def main():
    """Run one phase and record it in the private fixture file."""
    require_disposable_database()
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    if phase == "burst":
        fixtures["limits_state"] = burst_phase()
    elif phase == "settle":
        fixtures["limits"] = settle_phase(fixtures.pop("limits_state"))
    else:
        raise SystemExit("usage: limits_checks burst|settle")
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print(f"Limits control phase {phase} recorded.")


if __name__ == "__main__":
    main()
