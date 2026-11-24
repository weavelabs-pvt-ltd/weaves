"""Slice 5.3 control T5-I03: the usage ledger equals the run rows, per tenant.

Real stack, no testing endpoint. ``start`` runs real tasks through the
independent native worker (the fake model reports 10 + 10 tokens), then proves:
every succeeded agent run has exactly one settled ledger row with the same
tokens, the daily rollup equals the ledger, ``GET /usage`` and the real
operator script report each tenant only its own numbers, the restricted
database role cannot read another tenant's usage, and replaying a reserve or a
settle never counts twice. It also queues one workflow-trigger job whose model
call the fake provider holds open. The runner then SIGKILLs the worker; ``finish``
restarts nothing itself and waits for the existing job lease fence plus the
reconciler tick to move that reservation to ``unknown`` once, without counting
tokens that were never reported.
"""

import contextlib
import importlib.util
import io
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg

from tenancy_harness.client import API, call, request
from tenancy_harness.fakes import HOLD_MARKER
from tenancy_harness.live import wait_for
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T5-I03"
SLICE = "5.3"
SCRIPT = Path("/suite/operator/usage_report.py")
TOKENS = (10, 10)  # what the fake provider reports for every call
FIXTURE = "usage_ledger"


def rows(query, *params):
    """Read with the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(query, params).fetchall()


def restricted(org_id, query, *params):
    """Read as the restricted application role bound to one tenant (or none)."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "SELECT set_config('role', 'weaves_app', true), "
            "set_config('app.org_id', %s, true), "
            "set_config('app.system_purpose', '', true)",
            (org_id,),
        )
        return connection.execute(query, params).fetchall()


def run_tasks(tenants):
    """Real tasks through the independent worker: two for A, one for B."""
    done = []
    for label, count in (("a", 2), ("b", 1)):
        t = tenants[label]
        for index in range(count):
            task = call(
                "POST",
                "/tasks",
                t["admin"],
                {
                    "agent_id": t["agent"]["id"],
                    "title": f"usage-{label}-{index}",
                    "initial_message": f"usage ledger {label} {index}",
                },
            )
            done.append((t, task["task_id"]))
    for t, task_id in done:
        wait_for("/tasks/" + task_id, t["admin"], "latest_turn_status", "succeeded")


def run_records(org_id):
    """Succeeded agent runs (id -> tokens) and the org's ledger rows by key."""
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
    ledger = {
        r[0]: (r[1], r[2], r[3])
        for r in rows(
            "SELECT reservation_key, state, input_tokens, output_tokens "
            "FROM usage_ledger WHERE org_id = %s",
            org_id,
        )
    }
    return runs, ledger


def ledger_checks(tenants):
    """The ledger and its rollup equal the run rows for each tenant."""
    result = {}
    for label, t in tenants.items():
        runs, ledger = run_records(t["org_id"])
        settled = {k: (v[1], v[2]) for k, v in ledger.items() if v[0] == "settled"}
        sums = rows(
            "SELECT coalesce(sum(calls), 0), coalesce(sum(input_tokens), 0), "
            "coalesce(sum(output_tokens), 0) FROM usage_daily WHERE org_id = %s",
            t["org_id"],
        )[0]
        total = (len(settled), sum(v[0] for v in settled.values()))
        result[label] = {
            "equal": bool(runs) and settled == runs,
            "exact": all(tokens == TOKENS for tokens in runs.values()),
            "rollup": (sums[0], sums[1]) == total
            and sums[2] == sum(v[1] for v in settled.values()),
            "total_tokens": sum(sum(v) for v in settled.values()),
            "calls": len(settled),
        }
    return {
        "ledger_row_per_succeeded_run_with_equal_tokens": all(
            r["equal"] for r in result.values()
        ),
        "tokens_are_exactly_what_the_provider_reported": all(
            r["exact"] for r in result.values()
        ),
        "daily_rollup_equals_ledger": all(r["rollup"] for r in result.values()),
    }, {
        label: {"tokens": r["total_tokens"], "calls": r["calls"]}
        for label, r in result.items()
    }


def api_checks(tenants, totals):
    """``GET /usage`` is organization scoped and Owner/Admin only."""
    seen = {}
    for label, t in tenants.items():
        response = request("GET", "/usage", t["admin"]["headers"])
        seen[label] = response.json() if response.status_code == 200 else {}
    member = request("GET", "/usage", tenants["a"]["user"]["headers"])
    extra = request("GET", "/usage?bogus=1", tenants["a"]["admin"]["headers"])
    wide = request("GET", "/usage?from=2020-01-01", tenants["a"]["admin"]["headers"])
    return {
        "usage_endpoint_matches_own_ledger": all(
            seen[label].get("totals", {}).get("total_tokens") == totals[label]["tokens"]
            and seen[label].get("totals", {}).get("calls") == totals[label]["calls"]
            for label in tenants
        ),
        "usage_endpoint_is_scoped_to_the_organization": all(
            totals[label]["calls"] > 0 for label in tenants
        )
        and seen["a"].get("totals", {}).get("calls")
        + seen["b"].get("totals", {}).get("calls")
        == totals["a"]["calls"] + totals["b"]["calls"],
        "member_is_forbidden_and_bad_queries_rejected": member.status_code == 403
        and extra.status_code == 422
        and wide.status_code == 422,
    }


def isolation_checks(tenants):
    """Row security: one tenant never sees another's ledger or rollup."""
    a, b = tenants["a"]["org_id"], tenants["b"]["org_id"]
    own = restricted(a, "SELECT DISTINCT org_id FROM usage_ledger")
    other = restricted(a, "SELECT 1 FROM usage_ledger WHERE org_id = %s", b)
    daily = restricted(a, "SELECT DISTINCT org_id FROM usage_daily")
    nobody = restricted("", "SELECT 1 FROM usage_ledger")
    return {
        "restricted_role_sees_only_its_own_usage": own == [(a,)]
        and daily == [(a,)]
        and not other
        and not nobody,
    }


def replay_checks(tenants):
    """Replaying a reserve or settle (retry, redelivery) never counts twice."""
    from application.ports.usage import UsageReservationRequest, UsageSettlement
    from apps.runtime_wiring import open_process_runtime

    t = tenants["a"]
    key, ledger_row = next(iter(run_records(t["org_id"])[1].items()))
    before = rows(
        "SELECT sum(calls), sum(input_tokens), sum(output_tokens) FROM usage_daily "
        "WHERE org_id = %s",
        t["org_id"],
    )
    runtime = open_process_runtime(process="cli")
    now = datetime.now(UTC)
    try:
        ledger = runtime.usage_ledger
        context = rows(
            "SELECT workspace_id, run_id, agent_id FROM usage_ledger "
            "WHERE org_id = %s AND reservation_key = %s",
            t["org_id"],
            key,
        )[0]
        from shared.request_context import (
            reset_request_principal,
            set_request_identity,
        )

        scope = set_request_identity("harness", t["org_id"], context[0])
        try:
            again = ledger.reserve(
                UsageReservationRequest(
                    org_id=t["org_id"],
                    workspace_id=context[0],
                    reservation_key=key,
                    agent_run_id=key,
                    run_id=context[1],
                    agent_id=context[2],
                    provider_id=None,
                    model_id=None,
                    reserved_input_tokens=1,
                    reserved_output_tokens=1,
                    reserved_cost_nano_usd=None,
                    now=now,
                ),
                None,
            )
            redelivered = ledger.settle(
                UsageSettlement(
                    org_id=t["org_id"],
                    workspace_id=context[0],
                    reservation_key=key,
                    agent_run_id=key,
                    run_id=context[1],
                    agent_id=context[2],
                    provider_id=None,
                    model_id=None,
                    input_tokens=999,
                    output_tokens=999,
                    estimated_cost_nano_usd=None,
                    outcome="replay",
                    now=now,
                )
            )
        finally:
            reset_request_principal(scope)
    finally:
        runtime.close()
    after = rows(
        "SELECT sum(calls), sum(input_tokens), sum(output_tokens) FROM usage_daily "
        "WHERE org_id = %s",
        t["org_id"],
    )
    return {
        "replayed_reserve_is_a_noop": not again.created,
        "replayed_settle_never_double_counts": redelivered is False
        and before == after
        and run_records(t["org_id"])[1][key] == ledger_row,
    }


def operator_report(*argv):
    """Run the real operator script; return (status, rows)."""
    spec = importlib.util.spec_from_file_location("usage_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = module.run(list(argv))
    return status, json.loads(out.getvalue() or "[]")


def operator_checks(tenants, totals):
    """The business report: per-organization tokens and cost, owner connection."""
    status, report = operator_report("--by", "month")
    mine = {r["org_id"]: r for r in report}
    a, b = tenants["a"]["org_id"], tenants["b"]["org_id"]
    return {
        "operator_report_matches_ledger": status == 0
        and mine.get(a, {}).get("total_tokens") == totals["a"]["tokens"]
        and mine.get(b, {}).get("total_tokens") == totals["b"]["tokens"]
        and mine[a]["org_name"] == tenants["a"]["marker"],
        "operator_report_has_no_content": "initial_message" not in json.dumps(report),
    }


def hold_call(tenants):
    """Queue a webhook job whose model call is held open; return its identity."""
    t = tenants["a"]
    trigger = call(
        "POST",
        "/workflows/" + t["workflow"]["definition"]["workflow_id"] + "/triggers",
        t["admin"],
        {"name": "usage crash", "task_instructions": HOLD_MARKER},
    )
    sent = httpx.post(
        API + trigger["webhook_url"].removeprefix("/api/v0"),
        headers={
            "X-Workflow-Trigger-Secret": trigger["secret"],
            "X-Event-ID": "usage-crash",
        },
        json={"fixture": "usage"},
        timeout=30,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        held = rows(
            "SELECT reservation_key, state FROM usage_ledger WHERE org_id = %s "
            "AND state = 'reserved'",
            t["org_id"],
        )
        if held:
            return {"key": held[0][0], "queued": sent.status_code < 300}
        time.sleep(0.5)
    return {"key": None, "queued": sent.status_code < 300}


def unknown_rows(org_id):
    """Ledger rows already in the unknown state (other controls may add some)."""
    return int(
        rows(
            "SELECT count(*) FROM usage_ledger WHERE org_id = %s AND state = 'unknown'",
            org_id,
        )[0][0]
    )


def daily_total(org_id):
    """Rollup tokens of one organization."""
    return int(
        rows(
            "SELECT coalesce(sum(input_tokens + output_tokens), 0) FROM usage_daily "
            "WHERE org_id = %s",
            org_id,
        )[0][0]
    )


def start(tenants):
    """Settled-equals-ledger checks, then hold one call open for the kill."""
    checks, totals = ledger_checks(tenants)
    checks.update(api_checks(tenants, totals))
    checks.update(isolation_checks(tenants))
    checks.update(replay_checks(tenants))
    checks.update(operator_checks(tenants, totals))
    held = hold_call(tenants)
    return {
        **checks,
        "call_held_open_with_a_reservation": held["queued"] and held["key"] is not None,
        "_held": held,
        "_before": daily_total(tenants["a"]["org_id"]),
        "_unknown_before": unknown_rows(tenants["a"]["org_id"]),
    }


def finish(tenants, state):
    """After the worker was SIGKILLed: the lease expires, the row turns unknown."""
    org_id, key = tenants["a"]["org_id"], state["_held"]["key"]
    outcome = None
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline and key:
        found = rows(
            "SELECT state, outcome FROM usage_ledger WHERE org_id = %s "
            "AND reservation_key = %s",
            org_id,
            key,
        )
        if found and found[0][0] != "reserved":
            outcome = found[0]
            break
        time.sleep(2)
    expected_unknown = state["_unknown_before"] + 1
    summary = request("GET", "/usage", tenants["a"]["admin"]["headers"]).json()
    report = operator_report("--by", "month", "--org", org_id)[1]
    return {
        "_observed": {
            "outcome": outcome,
            "api_unknown": summary.get("unknown_calls"),
            "report": report,
        },
        "killed_worker_reservation_becomes_unknown_once": outcome
        == ("unknown", "lease_expired"),
        "unknown_call_counts_no_tokens": daily_total(org_id) == state["_before"],
        "unknown_call_is_reported_not_hidden": summary.get("unknown_calls")
        == expected_unknown
        and bool(report)
        and report[0]["unknown_calls"] == expected_unknown,
        "reconciler_does_not_repeat": rows(
            "SELECT count(*) FROM usage_ledger WHERE org_id = %s AND "
            "reservation_key = %s",
            org_id,
            key,
        )[0][0]
        == 1,
    }


def main(argv):
    """Record the control in the private fixture file the browser suite reads."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    tenants = fixtures["tenants"]
    if argv == ["start"]:
        run_tasks(tenants)
        fixtures[FIXTURE] = start(tenants)
    elif argv == ["finish"]:
        state = fixtures[FIXTURE]
        fixtures[FIXTURE] = {**state, **finish(tenants, state)}
    else:
        raise SystemExit("usage: usage_checks start|finish")
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    failed = [k for k, v in fixtures[FIXTURE].items() if v is False]
    print("Usage ledger control stage recorded:", *argv, "failing:", failed)
    if failed:
        print(json.dumps(fixtures[FIXTURE].get("_observed"), default=str))


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
