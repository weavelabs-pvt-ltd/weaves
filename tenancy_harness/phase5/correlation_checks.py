"""Slice 5.5 control T5-I05: a request id flows from API response to worker log.

Runs in the seed container after the native worker is up. Tenant A creates a task
with a caller-chosen ``X-Request-Id`` (and a prompt sentinel). The control then
checks, against durable rows, that the same id is on the persisted turn and on
the run's audit trail, never on another tenant's rows, that a malformed inbound
id is replaced, and that the internal metrics route answers only on the private
network and exposes no tenant. The structured log stream itself (API line, worker
line, no secret or prompt anywhere) is checked on the host by
``correlation_logs.verify`` because only the host can read container logs. The
probe the host needs is stored under ``fixtures["correlation"]["probe"]``.
"""

import json
import os
import secrets
import time
from pathlib import Path

import httpx
import psycopg

from tenancy_harness.client import API, request
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T5-I05"
SLICE = "5.5"
METRICS = "/internal/metrics"
# Identifier headers (organization, workspace, origin) are not credentials.
CREDENTIAL_HEADERS = {"cookie", "authorization", "x-weaves-csrf"}


def secret_values(*tenants):
    """Every credential-bearing header value the tenants' actors send."""
    values = set()
    for tenant in tenants:
        for actor_name in ("admin", "builder", "user"):
            headers = tenant.get(actor_name, {}).get("headers", {})
            for name, value in headers.items():
                if name.lower() in CREDENTIAL_HEADERS and len(value) >= 8:
                    values.add(value)
    return sorted(values)


def rows(query, *params):
    """Read on the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(query, params).fetchall()


def wait_for_turn(task_id, org_id, seconds=90):
    """The turn row once the worker has finished it, else None."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = rows(
            "SELECT payload FROM product_records WHERE collection = 'task_turns' "
            "AND org_id = %s AND payload->>'task_id' = %s",
            org_id,
            task_id,
        )
        if found and found[0][0]["status"] in {"succeeded", "failed", "cancelled"}:
            return found[0][0]
        time.sleep(0.25)
    return None


def create_task(tenant, request_id, prompt):
    """POST a task as tenant A's admin with a caller-chosen request id."""
    headers = {**tenant["admin"]["headers"], "X-Request-Id": request_id}
    return request(
        "POST",
        "/tasks",
        headers,
        {
            "agent_id": tenant["agent"]["id"],
            "title": "T5-I05 correlation",
            "initial_message": prompt,
        },
    )


def inbound_id_checks(tenant):
    """Well-formed ids are echoed; malformed or hostile ones are replaced."""
    headers = tenant["admin"]["headers"]
    replaced = {}
    for label, value in {
        "spaces": "bad id with spaces",
        "markup": "<script>alert(1)</script>",
        "too_long": "x" * 200,
        "too_short": "abc",
    }.items():
        response = request("GET", "/agents", {**headers, "X-Request-Id": value})
        sent = response.headers.get("X-Request-Id", "")
        replaced[label] = sent != value and len(sent) == 36
    return {"malformed_inbound_ids_are_replaced": all(replaced.values())}


def metrics_checks(a, b):
    """Direct peers read metrics; any proxied request gets a plain 404; no tenant."""
    direct = httpx.get(API + METRICS, timeout=30)
    forwarded = httpx.get(
        API + METRICS, headers={"X-Forwarded-For": "203.0.113.7"}, timeout=30
    )
    # The harness gateway (nginx) adds X-Forwarded-Proto; Caddy adds X-Forwarded-For.
    gateway = httpx.get(
        API + METRICS, headers={"X-Forwarded-Proto": "http"}, timeout=30
    )
    text = direct.text
    private = [a["org_id"], b["org_id"], a["marker"], b["marker"]]
    return {
        "metrics_served_to_direct_peers": direct.status_code == 200
        and "http_request_seconds_count" in text,
        "metrics_hidden_behind_a_proxy": forwarded.status_code == 404
        and gateway.status_code == 404,
        "metrics_have_no_tenant_identifiers": not any(item in text for item in private),
    }


def durable_checks(a, b, request_id, task_id, response):
    """Turn and audit rows carry the id for tenant A only."""
    turn = wait_for_turn(task_id, a["org_id"])
    carriers = rows(
        "SELECT DISTINCT org_id FROM product_records WHERE payload::text LIKE %s",
        "%" + request_id + "%",
    )
    audit = rows(
        "SELECT count(*) FROM product_records WHERE collection = 'audit_events' "
        "AND org_id = %s AND payload->>'correlation_id' = %s",
        a["org_id"],
        request_id,
    )[0][0]
    return {
        "response_echoes_the_request_id": response.status_code == 202
        and response.headers.get("X-Request-Id") == request_id,
        "turn_finished_in_the_worker": turn is not None,
        "turn_stores_the_request_id": bool(turn)
        and turn.get("request_id") == request_id,
        "run_audit_trail_has_the_request_id": audit > 0,
        "request_id_never_on_another_tenant": [row[0] for row in carriers]
        == [a["org_id"]]
        and b["org_id"] not in [row[0] for row in carriers],
    }, turn


def correlation_control(fixtures):
    """Run the database and HTTP half of T5-I05 and describe the log probe."""
    a, b = fixtures["tenants"]["a"], fixtures["tenants"]["b"]
    request_id = "t5i05-" + secrets.token_hex(8)
    prompt = "T5I05-PROMPT-SENTINEL-" + secrets.token_hex(8)
    response = create_task(a, request_id, prompt)
    task_id = response.json().get("task_id", "") if response.status_code == 202 else ""
    result, turn = durable_checks(a, b, request_id, task_id, response)
    result.update(inbound_id_checks(a))
    result.update(metrics_checks(a, b))
    result["probe"] = {
        "request_id": request_id,
        "turn_id": (turn or {}).get("turn_id", ""),
        "org_id": a["org_id"],
        "other_org_id": b["org_id"],
        "prompt": prompt,
        "secrets": secret_values(a, b),
    }
    return result


def main():
    """Record the control in the private fixture file the browser suite reads."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    fixtures["correlation"] = correlation_control(fixtures)
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Correlation control recorded.")


if __name__ == "__main__":
    main()
