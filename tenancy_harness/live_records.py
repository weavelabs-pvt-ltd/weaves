"""Retain private live diagnostics before validating durable model provenance."""

import json
import os

import psycopg

from tenancy_harness.client import call


def capture_records(task_ids, tenant, path):
    """Save synthetic model responses privately before any assertions can fail."""
    records = []
    try:
        with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
            for task_id in task_ids:
                records.append(_record(connection, task_id, tenant))
    finally:
        path.write_text(json.dumps(records, indent=2))
        path.chmod(0o600)
    return records


def _record(connection, task_id, tenant):
    """Project only scoped synthetic response/provenance fields, never credentials."""
    org_id = tenant["org_id"]
    task = call("GET", "/tasks/" + task_id, tenant["admin"])
    turn = connection.execute(
        "SELECT payload FROM product_records WHERE collection='task_turns' "
        "AND org_id=%s AND payload->>'task_id'=%s",
        (org_id, task_id),
    ).fetchone()[0]
    run_row = connection.execute(
        "SELECT payload FROM product_records WHERE collection='agent_runs' "
        "AND org_id=%s AND record_id=%s",
        (org_id, turn["agent_run_id"]),
    ).fetchone()
    run = run_row[0] if run_row else {}
    version_row = connection.execute(
        "SELECT payload FROM product_records WHERE collection='agent_versions' "
        "AND org_id=%s AND record_id=%s",
        (org_id, run.get("agent_version_id")),
    ).fetchone()
    version = version_row[0] if version_row else {}
    tools = connection.execute(
        "SELECT payload->>'status' FROM product_records "
        "WHERE collection='tool_invocations' AND org_id=%s "
        "AND payload->>'agent_run_id'=%s",
        (org_id, run.get("agent_run_id")),
    ).fetchall()
    usage = connection.execute(
        "SELECT state, input_tokens, output_tokens, estimated_cost_nano_usd, "
        "model_id FROM usage_ledger WHERE org_id=%s AND reservation_key=%s",
        (org_id, run.get("agent_run_id")),
    ).fetchone()
    return {
        "task_id": task_id,
        "usage_ledger": usage,
        "status": task["latest_turn_status"],
        "model": run.get("resolved_model_id"),
        "provider_id": run.get("resolved_provider_id"),
        "summary": run.get("output_summary") or {},
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "skill_pins": [
            s.get("source_skill_version_id") for s in version.get("skills", [])
        ],
        "tool_statuses": [row[0] for row in tools],
        "error": {"turn": turn.get("error"), "run": run.get("error")},
    }


def check_ledger(record):
    """The product's usage ledger holds exactly the provider-reported tokens."""
    ledger = record["usage_ledger"]
    if not ledger or ledger[0] != "settled":
        raise AssertionError("Live call is not settled in the usage ledger")
    if (ledger[1], ledger[2]) != (record["input_tokens"], record["output_tokens"]):
        raise AssertionError("Usage ledger tokens differ from the provider usage")


def verify_records(records, live, skill_version_id):
    """Require a real agent response, usage, pinned skill and successful tool call."""
    if len(records) != 3:
        raise AssertionError("Live smoke exceeded or missed its three planned calls")
    calls = []
    for record in records:
        summary = record["summary"]
        if record["status"] != "succeeded" or live["marker"] not in summary.get(
            "response", ""
        ):
            raise AssertionError(
                "Live model did not produce the requested response; "
                "inspect private live-records.json"
            )
        if record["provider_id"] not in live["provider_ids"]:
            raise AssertionError("Live model execution used an unexpected provider")
        if not summary.get("citations"):
            raise AssertionError("Live agent did not record connector citations")
        if not record["input_tokens"] or not record["output_tokens"]:
            raise AssertionError("Live provider usage was not recorded")
        check_ledger(record)
        if skill_version_id not in record["skill_pins"]:
            raise AssertionError("Live execution lost its pinned organization skill")
        if record["tool_statuses"] != ["succeeded"]:
            raise AssertionError(
                "Live read-only connector invocation was not successful"
            )
        calls.append(
            {
                "model": record["model"],
                "input_tokens": record["input_tokens"],
                "output_tokens": record["output_tokens"],
                "ledger_input_tokens": record["usage_ledger"][1],
                "ledger_output_tokens": record["usage_ledger"][2],
                "ledger_cost_nano_usd": record["usage_ledger"][3],
                "citations": len(summary["citations"]),
                "skill_pinned": True,
                "tool_invocations": 1,
            }
        )
    return {"verified_model_calls": calls}
