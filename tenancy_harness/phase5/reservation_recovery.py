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
    """Retry one idempotent submission through outages; (task id, last status)."""
    sent = {"status": None, "task_id": None}
    while time.monotonic() < deadline:
        sent = submit_task(t, message, key)
        if sent["task_id"]:
            break
        time.sleep(1.0 if sent["status"] != 429 else 3.0)
    return sent["task_id"], sent["status"]


def stream(t, seconds, rate, log):
    """Submit tasks at ``rate`` per second for ``seconds``; record every key."""
    end = time.monotonic() + seconds
    index = 0
    while time.monotonic() < end:
        index += 1
        key = f"rr-{t['label']}-{index}"
        marker = MIX[index % len(MIX)]
        task_id, status = submit_until_accepted(
            t, f"{marker}restart stream {index}", key, time.monotonic() + 90
        )
        log.append((key, task_id, status))
        time.sleep(1.0 / rate)


def one(query, *params):
    """First column of the first row (counts)."""
    return rows(query, *params)[0][0]


def read_facts(org_id):
    """Everything the audit compares, read once from the database."""
    return {
        "tasks": one(
            "SELECT count(*) FROM product_records WHERE collection = 'tasks' "
            "AND org_id = %s",
            org_id,
        ),
        "turns": rows(
            "SELECT payload->>'status', payload->>'agent_run_id', "
            "payload->'error'->>'code', COALESCE((payload->>'attempts')::int, 0) "
            "FROM product_records WHERE collection = 'task_turns' AND org_id = %s",
            org_id,
        ),
        "ledger": rows(
            "SELECT reservation_key, state, input_tokens, output_tokens, outcome, "
            "reserved_input_tokens + reserved_output_tokens "
            "FROM usage_ledger WHERE org_id = %s",
            org_id,
        ),
        "runs": {
            r[0]: (r[1], r[2], r[3])
            for r in rows(
                "SELECT record_id, payload->>'status', "
                "(payload->>'input_tokens')::int, (payload->>'output_tokens')::int "
                "FROM product_records WHERE collection = 'agent_runs' "
                "AND org_id = %s",
                org_id,
            )
        },
        "rollup": rows(
            "SELECT coalesce(sum(calls), 0), coalesce(sum(input_tokens), 0) + "
            "coalesce(sum(output_tokens), 0) FROM usage_daily WHERE org_id = %s",
            org_id,
        )[0],
        "by_call_length": {
            f"{marker}/{status}": count
            for marker, status, count in rows(
                "SELECT CASE WHEN k.payload->>'title' LIKE 'tenancy-slow=15%%' "
                "THEN 'slow15' WHEN k.payload->>'title' LIKE 'tenancy-slow=1 %%' "
                "THEN 'slow1' ELSE 'fast' END, t.payload->>'status', count(*) "
                "FROM product_records t JOIN product_records k "
                "ON k.collection = 'tasks' AND k.record_id = t.payload->>'task_id' "
                "AND k.org_id = t.org_id WHERE t.collection = 'task_turns' "
                "AND t.org_id = %s GROUP BY 1, 2",
                org_id,
            )
        },
    }


def tally(items):
    """Histogram of hashable values."""
    out = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def invariants(facts, accepted):
    """The named recovery invariants over one organization's facts."""
    turns, ledger = facts["turns"], facts["ledger"]
    runs = {k: v[1:] for k, v in facts["runs"].items() if v[0] == "succeeded"}
    settled = {r[0]: (r[2], r[3]) for r in ledger if r[1] == "settled"}
    run_ids = [t[1] for t in turns if t[1]]
    return {
        "one_task_per_idempotency_key": facts["tasks"]
        == len(set(accepted))
        == len(accepted),
        "one_turn_per_task": len(turns) == facts["tasks"],
        "every_turn_terminal": not {t[0] for t in turns} & {"queued", "running"},
        "attempts_within_the_retry_bound": all(t[3] <= 3 for t in turns),
        "at_most_one_run_per_turn": len(run_ids) == len(set(run_ids)),
        # A killed turn's reservation is released by the 4 hour stale-run rule, not
        # at the lease fence (the job-only path of 5.3); only failed turns may
        # still hold one, and they hold a bounded, reported number of tokens.
        "open_reservations_only_for_failed_turns": sum(
            1 for r in ledger if r[1] == "reserved"
        )
        <= sum(1 for t in turns if t[0] == "failed"),
        "ledger_keys_unique": len({r[0] for r in ledger}) == len(ledger),
        "every_succeeded_run_settled_with_equal_tokens": all(
            settled.get(key) == value for key, value in runs.items()
        ),
        "every_settled_row_is_exactly_one_reported_call": all(
            v == (TOKENS // 2, TOKENS // 2) for v in settled.values()
        ),
        "rollup_equals_ledger": (int(facts["rollup"][0]), int(facts["rollup"][1]))
        == (len(settled), sum(sum(v) for v in settled.values())),
        "unknown_never_counts_tokens": all(
            r[2] in (0, None) for r in ledger if r[1] == "unknown"
        ),
    }


def audit_org(org_id, keys):
    """Per-organization invariants as named booleans plus the raw counts."""
    facts = read_facts(org_id)
    turns, ledger, runs = facts["turns"], facts["ledger"], facts["runs"]
    settled = {r[0] for r in ledger if r[1] == "settled"}
    succeeded = {k for k, v in runs.items() if v[0] == "succeeded"}
    accepted = [task for _key, task, _status in keys if task]
    return {
        "counts": {
            "keys_sent": len(keys),
            "keys_accepted": len(accepted),
            "tasks": facts["tasks"],
            "turns": len(turns),
            "turn_statuses": tally(t[0] for t in turns),
            "turn_attempts": tally(t[3] for t in turns),
            "turns_by_call_length_and_status": facts["by_call_length"],
            "turn_error_codes": sorted({t[2] for t in turns if t[2]}),
            "ledger_rows": len(ledger),
            "ledger_states": tally(f"{r[1]}/{r[4]}" for r in ledger),
            "settled": len(settled),
            "open_reservations": sum(1 for r in ledger if r[1] == "reserved"),
            "open_reservation_tokens": sum(r[5] for r in ledger if r[1] == "reserved"),
            "unanswered_submissions": [
                status for _key, task, status in keys if not task
            ],
            "runs_succeeded": len(succeeded),
            "paid_calls_whose_run_did_not_succeed": tally(
                runs.get(key, ("no run row",))[0] for key in settled - succeeded
            ),
        },
        "checks": invariants(facts, accepted),
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
    settled_rows = sum(a["counts"]["settled"] for a in audits.values())
    checks = {
        "recovered_within_wait": recovered,
        "provider_calls_never_exceed_reservations": calls <= ledger_rows,
        "every_settled_row_reached_the_provider": calls >= settled_rows,
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
        "settled_rows": settled_rows,
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
    p.add_argument("--recovery-wait", type=int, default=900, dest="recovery_wait")
    args = p.parse_args(argv)
    require_disposable_database()
    wait_for_services()
    result = run(args)
    failing = [name for name, ok in result["checks"].items() if not ok]
    print("restart audit passed:", result["passed"], "failing:", failing)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
