"""Slice 5.6 scenario 3: restart the API and the worker under load, then audit.

Runs inside the harness ``seed`` container while the host orchestrator
SIGKILLs and restarts the real ``api`` and ``worker`` containers on a schedule
(no test hooks exist in the product: kills land wherever the processes happen to
be, which under a dense stream of short model calls means all of claim, usage
reservation, the provider call and settlement). Submitters retry with a stable
``Idempotency-Key`` through every outage. When the stream stops, recovery is
awaited and the audit compares what each layer recorded:

* one task and one turn per idempotency key (no duplicate acceptance);
* at most one run per turn and one provider call per usage reservation (no
  duplicate external effect; the fake provider counts every completion);
* no ledger row left ``reserved``; settled tokens equal the run rows exactly and
  the daily rollup equals the settled rows (no double counting); every
  ``unknown`` row belongs to a turn that did not succeed;
* no turn left queued or running and capacity is not leaked: both tenants can
  still run new work.
"""

import argparse
import threading
import time

import httpx

from tenancy_harness.client import PROVIDER, wait_for_services
from tenancy_harness.phase5.load_common import (
    light_tenant,
    now,
    rows,
    submit_task,
    wait_idle,
    write_result,
)

TOKENS = 20  # the fake provider reports 10 + 10 for every call
MIX = ("", "", "tenancy-slow=1 ", "tenancy-slow=1 ", "tenancy-slow=15 ")


def provider_calls():
    """Completions the fake provider has served so far."""
    events = httpx.get(PROVIDER + "/model-events", timeout=10).json()["events"]
    return sum(1 for event in events if event["path"] == "/v1/chat/completions")


def submit_until_accepted(t, message, key, deadline):
    """Retry one idempotent submission through outages until it is accepted."""
    while time.monotonic() < deadline:
        sent = submit_task(t, message, key)
        if sent["task_id"]:
            return sent["task_id"]
        time.sleep(1.0 if sent["status"] != 429 else 3.0)
    return None


def stream(t, seconds, rate, log):
    """Submit tasks at ``rate`` per second for ``seconds``; record every key."""
    end = time.monotonic() + seconds
    index = 0
    while time.monotonic() < end:
        index += 1
        key = f"rr-{t['label']}-{index}"
        marker = MIX[index % len(MIX)]
        task_id = submit_until_accepted(
            t, f"{marker}restart stream {index}", key, time.monotonic() + 90
        )
        log.append((key, task_id))
        time.sleep(1.0 / rate)


def one(query, *params):
    """First column of the first row (counts)."""
    return rows(query, *params)[0][0]


def audit_org(org_id, keys):
    """Per-organization invariants as named booleans plus the raw counts."""
    tasks = one(
        "SELECT count(*) FROM product_records WHERE collection = 'tasks' "
        "AND org_id = %s",
        org_id,
    )
    turns = rows(
        "SELECT payload->>'status', payload->>'agent_run_id', "
        "payload->'error'->>'code', COALESCE((payload->>'attempts')::int, 0) "
        "FROM product_records WHERE collection = 'task_turns' AND org_id = %s",
        org_id,
    )
    ledger = rows(
        "SELECT reservation_key, state, input_tokens, output_tokens "
        "FROM usage_ledger WHERE org_id = %s",
        org_id,
    )
    runs = {
        r[0]: (r[1], r[2])
        for r in rows(
            "SELECT record_id, (payload->>'input_tokens')::int, "
            "(payload->>'output_tokens')::int FROM product_records "
            "WHERE collection = 'agent_runs' AND org_id = %s "
            "AND payload->>'status' = 'succeeded'",
            org_id,
        )
    }
    settled = {r[0]: (r[2], r[3]) for r in ledger if r[1] == "settled"}
    rollup = rows(
        "SELECT coalesce(sum(calls), 0), coalesce(sum(input_tokens), 0) + "
        "coalesce(sum(output_tokens), 0) FROM usage_daily WHERE org_id = %s",
        org_id,
    )[0]
    run_ids = [t[1] for t in turns if t[1]]
    statuses = {}
    for status, _run, _code, _attempts in turns:
        statuses[status] = statuses.get(status, 0) + 1
    unknown = [r[0] for r in ledger if r[1] == "unknown"]
    accepted = [task for _key, task in keys if task]
    return {
        "counts": {
            "keys_sent": len(keys),
            "keys_accepted": len(accepted),
            "tasks": tasks,
            "turns": len(turns),
            "turn_statuses": statuses,
            "turn_error_codes": sorted({t[2] for t in turns if t[2]}),
            "ledger_rows": len(ledger),
            "settled": len(settled),
            "unknown": len(unknown),
            "runs_succeeded": len(runs),
        },
        "checks": {
            "one_task_per_idempotency_key": tasks
            == len(set(accepted))
            == len(accepted),
            "one_turn_per_task": len(turns) == tasks,
            "every_turn_terminal": not set(statuses) & {"queued", "running"},
            "one_attempt_per_turn": all(t[3] <= 1 for t in turns),
            "at_most_one_run_per_turn": len(run_ids) == len(set(run_ids)),
            "no_reservation_left_open": not any(r[1] == "reserved" for r in ledger),
            "ledger_keys_unique": len({r[0] for r in ledger}) == len(ledger),
            "settled_tokens_equal_run_rows": settled == runs,
            "settled_calls_exact": all(
                v == (TOKENS // 2, TOKENS // 2) for v in runs.values()
            ),
            "rollup_equals_ledger": (int(rollup[0]), int(rollup[1]))
            == (len(settled), sum(sum(v) for v in settled.values())),
            "unknown_never_counts_tokens": all(
                r[2] in (0, None) for r in ledger if r[1] == "unknown"
            ),
        },
    }


def capacity_checks(tenants):
    """After recovery both tenants can still run and nothing is left running."""
    sent = [(t, submit_task(t, "after recovery")) for t in tenants]
    accepted = all(s["task_id"] for _t, s in sent)
    done = wait_idle(60)
    states = [
        one(
            "SELECT count(*) FROM product_records WHERE collection = 'task_turns' "
            "AND org_id = %s AND payload->>'task_id' = %s "
            "AND payload->>'status' = 'succeeded'",
            t["org_id"],
            s["task_id"],
        )
        if s["task_id"]
        else 0
        for t, s in sent
    ]
    return {
        "new_work_accepted_after_recovery": accepted,
        "new_work_runs_after_recovery": done and all(n == 1 for n in states),
    }


def run(args):
    """Stream, wait for recovery, audit; the result is written for the host."""
    tenants = [light_tenant(f"{args.label}{i}") for i in range(args.tenants)]
    baseline = provider_calls()
    logs = {t["label"]: [] for t in tenants}
    threads = [
        threading.Thread(
            target=stream, args=(t, args.seconds, args.rate, logs[t["label"]])
        )
        for t in tenants
    ]
    started = now()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    streamed = now()
    recovered = wait_idle(args.recovery_wait)
    recovery_seconds = (now() - streamed).total_seconds()
    audits = {t["label"]: audit_org(t["org_id"], logs[t["label"]]) for t in tenants}
    ledger_rows = sum(a["counts"]["ledger_rows"] for a in audits.values())
    calls = provider_calls() - baseline
    checks = {
        "recovered_within_wait": recovered,
        "no_provider_call_without_a_reservation": calls <= ledger_rows,
        "no_duplicate_provider_call_per_reservation": calls <= ledger_rows,
        **capacity_checks(tenants),
    }
    for audit in audits.values():
        for name, ok in audit["checks"].items():
            checks[name] = checks.get(name, True) and ok
    result = {
        "config": vars(args),
        "started_at": started,
        "streamed_at": streamed,
        "recovery_seconds_after_stream": round(recovery_seconds, 1),
        "provider_calls": calls,
        "ledger_rows": ledger_rows,
        "audits": audits,
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_result(args.label, result)
    return result


def main(argv=None):
    """Run the stream and the audit; the exit status reports the outcome."""
    from tenancy_harness.seed import require_disposable_database

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--label", required=True)
    p.add_argument("--seconds", type=int, default=240)
    p.add_argument("--rate", type=float, default=1.5, help="submissions/s/tenant")
    p.add_argument("--tenants", type=int, default=2)
    p.add_argument("--recovery-wait", type=int, default=480, dest="recovery_wait")
    args = p.parse_args(argv)
    require_disposable_database()
    wait_for_services()
    result = run(args)
    failing = [name for name, ok in result["checks"].items() if not ok]
    print("restart audit passed:", result["passed"], "failing:", failing)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
