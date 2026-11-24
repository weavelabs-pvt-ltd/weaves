"""Slice 6.4 control T6-I03: runtime kill switches and tenant quarantine.

Real stack, no testing endpoint. The real operator scripts
(``organization_lifecycle.py quarantine`` and ``platform_control.py``, mounted
under ``/suite/operator``) run on the owner connection; the API and the
independent native worker keep running and are never restarted.

Quarantine: tenant A gets a schedule and a webhook trigger. After
``quarantine`` its API calls and webhook are refused, its schedule and trigger
are disabled in the database and tenant B keeps working (a task completes in the
worker, B's rows are untouched). ``release-quarantine`` restores A, re-enables
exactly what was disabled and leaves B unchanged. Append-only audit rows exist for
every command, none for B, and the restricted database role cannot read them.

Switches: ``read_only`` makes the live API answer writes with 503
``platform_read_only`` while reads work; ``admissions_paused`` makes new work 429
``admissions_paused``; ``worker_paused`` leaves a queued task unclaimed until it
is cleared. Each takes effect within the 5 second cache (plus the worker poll)
without any restart and is undone by ``clear``.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path

import psycopg

from tenancy_harness.client import request
from tenancy_harness.resources import definitions, tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T6-I03"
SLICE = "6.4"
FIXTURE = "kill_switch"

LIFECYCLE = Path("/suite/operator/organization_lifecycle.py")
CONTROL = Path("/suite/operator/platform_control.py")
OP = ["--operator", "harness@tenancy.test", "--reason", "T6-I03 control"]
LATENCY_LIMIT = 9.0  # the 5 s cache plus polling slack


def script(path, name, *argv):
    """Run a real operator script in-process; return (status, JSON stdout)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = module.run(list(argv))
    text = out.getvalue().strip()
    return status, json.loads(text) if text else None


def lifecycle(*argv):
    """``organization_lifecycle.py`` as the operator would run it."""
    return script(LIFECYCLE, "organization_lifecycle", *argv)


def control(*argv):
    """``platform_control.py`` as the operator would run it."""
    return script(CONTROL, "platform_control", *argv)


def rows(query, *params):
    """Read durable rows on the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(query, params).fetchall()


def restricted(query):
    """Read as the restricted application role bound to no tenant."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "SELECT set_config('role', 'weaves_app', true), "
            "set_config('app.org_id', '', true), "
            "set_config('app.system_purpose', '', true)"
        )
        return connection.execute(query).fetchall()


def statuses(org_id, collection):
    """Sorted payload statuses of one organization's rows in a collection."""
    return sorted(
        row[0]
        for row in rows(
            "SELECT payload->>'status' FROM product_records "
            "WHERE org_id = %s AND collection = %s",
            org_id,
            collection,
        )
    )


def b_fingerprint(org_id):
    """A stable digest of everything B owns, excluding growing run history."""
    return rows(
        "SELECT collection, record_id, md5(payload::text) FROM product_records "
        "WHERE org_id = %s AND collection IN ('workflow_schedules', "
        "'workflow_triggers', 'agents', 'workflows', 'organizations', "
        "'organization_lifecycle') ORDER BY collection, record_id",
        org_id,
    )


def create_task(t, name):
    """POST one task; return the raw response."""
    return request(
        "POST",
        "/tasks",
        t["admin"]["headers"],
        {"agent_id": t["agent"]["id"], "title": name, "initial_message": name},
    )


def until(check, limit=LATENCY_LIMIT):
    """Seconds until ``check()`` is truthy, or None when it never was."""
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if check():
            return round(time.monotonic() - started, 1)
        time.sleep(0.25)
    return None


def drained(org_id, seconds=120):
    """Wait until an organization has no queued or running task turn or job."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        busy = rows(
            "SELECT count(*) FROM product_records WHERE org_id = %s "
            "AND collection IN ('task_turns', 'execution_jobs') "
            "AND payload->>'status' IN ('queued', 'running')",
            org_id,
        )[0][0]
        if busy == 0:
            return True
        time.sleep(0.5)
    return False


def succeeded(org_id):
    """How many task turns of an organization finished successfully."""
    return rows(
        "SELECT count(*) FROM product_records WHERE org_id = %s "
        "AND collection = 'task_turns' AND payload->>'status' = 'succeeded'",
        org_id,
    )[0][0]


def quarantine_results(a, b):
    """Quarantine A, prove B is unaffected, release, prove A is exact again."""
    wid = a["workflow"]["definition"]["workflow_id"]
    schedule = request(
        "POST",
        f"/workflows/{wid}/schedules",
        a["admin"]["headers"],
        {"name": "t6", "task_instructions": "t6", "interval_seconds": 86400},
    ).json()
    trigger = request(
        "POST",
        f"/workflows/{wid}/triggers",
        a["admin"]["headers"],
        {"name": "t6", "task_instructions": "t6"},
    ).json()
    hook = {
        "X-Event-ID": "t6-quarantine-1",
        "X-Workflow-Trigger-Secret": trigger["secret"],
    }
    b_before = b_fingerprint(b["org_id"])
    jobs_before = len(statuses(a["org_id"], "execution_jobs"))
    status, shown = lifecycle(
        "quarantine", "--organization-id", a["org_id"], *OP, "--ticket", "T6-I03"
    )
    blocked = {
        "agents": request("GET", "/agents", a["admin"]["headers"]).status_code,
        "tasks": create_task(a, "while quarantined").status_code,
        "webhook": request(
            "POST", trigger["webhook_url"][len("/api/v0") :], hook, {"e": 1}
        ).status_code,
    }
    b_task = create_task(b, "b while a is quarantined")
    b_done = (
        b_task.status_code == 202
        and drained(b["org_id"])
        and succeeded(b["org_id"]) >= 1
    )
    b_ok = request("GET", "/agents", b["admin"]["headers"]).status_code == 200
    again, _ = lifecycle("quarantine", "--organization-id", a["org_id"], *OP)
    quarantined = {
        "command_ok": status == 0 and shown["state"] == "suspended",
        "schedule_disabled": statuses(a["org_id"], "workflow_schedules")
        == ["disabled"],
        "trigger_disabled": statuses(a["org_id"], "workflow_triggers") == ["disabled"],
        "a_api_refused": blocked["agents"] == 403 and blocked["tasks"] == 403,
        "a_webhook_refused": blocked["webhook"] >= 400,
        "a_no_job_created": len(statuses(a["org_id"], "execution_jobs")) == jobs_before,
        "b_still_serves_and_completes": b_ok and b_done,
        "b_rows_untouched": b_fingerprint(b["org_id"]) == b_before,
        "repeat_is_idempotent": again == 0,
    }
    status, released = lifecycle(
        "release-quarantine", "--organization-id", a["org_id"], *OP
    )
    restored = {
        "release_ok": status == 0 and released["state"] == "active",
        "a_api_restored": request("GET", "/agents", a["admin"]["headers"]).status_code
        == 200,
        "schedule_reenabled": statuses(a["org_id"], "workflow_schedules") == ["active"],
        "trigger_reenabled": statuses(a["org_id"], "workflow_triggers") == ["active"],
        "a_webhook_works_again": request(
            "POST", trigger["webhook_url"][len("/api/v0") :], hook, {"e": 2}
        ).status_code
        == 202,
        "b_rows_untouched_after_release": b_fingerprint(b["org_id"]) == b_before,
        "schedule_still_the_same_record": schedule["schedule_id"]
        in {
            r[0]
            for r in rows(
                "SELECT record_id FROM product_records WHERE org_id = %s "
                "AND collection = 'workflow_schedules'",
                a["org_id"],
            )
        },
    }
    return quarantined, restored


def audit_results(a, b):
    """Every command left an append-only row; B has none; the app role sees none."""
    audited = rows(
        "SELECT command, operator, reason, correlation_id, result, ticket "
        "FROM operator_actions WHERE org_id = %s ORDER BY at",
        a["org_id"],
    )
    commands = [row[0] for row in audited]
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
            c.execute("DELETE FROM operator_actions")
        deletable = True
    except psycopg.Error:
        deletable = False
    return {
        "quarantine_and_release_are_audited": commands.count("organization.quarantine")
        == 2
        and commands.count("organization.release_quarantine") == 1,
        "audit_rows_name_operator_reason_and_correlation": all(
            row[1] == "harness@tenancy.test"
            and row[2] == "T6-I03 control"
            and row[3].startswith("op-")
            and row[4] == "ok"
            for row in audited
        ),
        "ticket_is_recorded": audited[0][5] == "T6-I03",
        "other_tenant_has_no_audit_rows": not rows(
            "SELECT 1 FROM operator_actions WHERE org_id = %s", b["org_id"]
        ),
        "audit_is_append_only": not deletable,
        "restricted_role_cannot_read_audit": restricted(
            "SELECT count(*) FROM operator_actions"
        )[0][0]
        == 0,
    }


def switch_results(b):
    """Flip each switch on the running stack and time how long it takes."""
    results, latency = {}, {}
    headers = b["admin"]["headers"]
    control("set", *OP, "--read-only", "true", "--message", "Maintenance window.")
    latency["read_only"] = until(lambda: create_task(b, "ro").status_code == 503)
    body = create_task(b, "ro").json()
    results["read_only_refuses_writes_with_a_clear_503"] = (
        latency["read_only"] is not None
        and body["code"] == "platform_read_only"
        and body["detail"] == "Maintenance window."
    )
    results["read_only_still_serves_reads"] = (
        request("GET", "/agents", headers).status_code == 200
    )
    control("clear", *OP)
    results["read_only_clears"] = (
        until(lambda: create_task(b, "after ro").status_code == 202) is not None
    )

    control("set", *OP, "--admissions-paused", "true")
    latency["admissions_paused"] = until(
        lambda: create_task(b, "paused").status_code == 429
    )
    refused = create_task(b, "paused").json()
    results["admissions_pause_refuses_new_work"] = (
        latency["admissions_paused"] is not None
        and refused["code"] == "limit_exceeded"
        and refused["resource"] == "admissions_paused"
    )
    control("clear", *OP)
    results["admissions_pause_clears"] = (
        until(lambda: create_task(b, "after pause").status_code == 202) is not None
    )

    drained(b["org_id"])  # nothing admitted so far may still be running
    control("set", *OP, "--worker-paused", "true")
    time.sleep(7)  # the worker's cache plus a poll
    held = create_task(b, "held by the paused worker")
    time.sleep(6)
    held_queued = held.status_code == 202 and "queued" in statuses(
        b["org_id"], "task_turns"
    )
    control("clear", *OP)
    results["worker_pause_leaves_work_queued"] = held_queued
    results["worker_pause_clears_and_work_completes"] = drained(
        b["org_id"]
    ) and "queued" not in statuses(b["org_id"], "task_turns")

    results["switches_need_no_restart"] = all(
        value is not None and value <= LATENCY_LIMIT for value in latency.values()
    )
    stored = rows(
        "SELECT read_only, admissions_paused, worker_paused FROM platform_controls"
    )
    results["clear_returns_to_environment_defaults"] = stored == [(None, None, None)]
    results["switch_changes_are_audited"] = (
        rows("SELECT count(*) FROM operator_actions WHERE command LIKE 'platform.%'")[
            0
        ][0]
        == 6
    )
    results["restricted_role_cannot_change_a_switch"] = (
        restricted("SELECT count(*) FROM platform_controls")[0][0] == 1
    )
    return results, latency


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    a, b = tenant("p6a"), tenant("p6b")
    for t in (a, b):
        definitions(t)
    try:
        quarantined, restored = quarantine_results(a, b)
        switches, latency = switch_results(b)
    finally:
        control("clear", *OP)  # never leave a switch set for later controls
    results = {
        **quarantined,
        **restored,
        **audit_results(a, b),
        **switches,
        "latency_seconds": latency,
    }
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    fixtures[FIXTURE] = results
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    failed = [k for k, v in results.items() if v is not True and k != "latency_seconds"]
    print(f"{CONTROL_ID} recorded; failing checks: {failed or 'none'}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
