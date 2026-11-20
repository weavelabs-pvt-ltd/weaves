"""Bounded live Anthropic smoke through real agent, skill, tool and workflow APIs."""

import json
import os
import sys
import time
from pathlib import Path

import httpx
import psycopg

from tenancy_harness.client import API, PROVIDER, call
from tenancy_harness.seed import require_disposable_database


def wait_for(resource, actor, field, expected):
    """Poll persisted state without retrying or creating additional model calls."""
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = call("GET", resource, actor)
        if result.get(field) == expected:
            return result
        if result.get(field) in {"failed", "cancelled"}:
            raise RuntimeError("Live execution failed; no automatic paid retry")
        time.sleep(0.5)
    raise RuntimeError("Live smoke timed out; no automatic paid retry")


def configured_agent(t, config, model, name):
    """Register a real provider key and pin the model, org skill and read-only tool."""
    provider = call(
        "POST",
        "/model-providers",
        t["admin"],
        {
            "provider_type": "anthropic",
            "display_name": "Live " + name,
            "api_key": config["api_key"],
            "initial_model": model,
            "base_url": PROVIDER + "/live/anthropic",
        },
    )
    profiles = call("GET", "/model-profiles", t["admin"])
    profile = next(
        p
        for p in profiles
        if p["provider_id"] == provider["id"] and p["model"] == model
    )
    agent = call(
        "POST",
        "/agents",
        t["admin"],
        {
            "name": "live-" + name,
            "model_profile_id": profile["id"],
            "instructions": "Follow the request; include its LIVE_CHECK marker. "
            "answer. Use context and cite it; do not take actions.",
            "allowed_capability_ids": t["installation"]["enabled_capability_ids"],
            "skill_version_ids": [t["org_skill"]["version"]["skill_version_id"]],
            "max_output_tokens": 256,
            "max_runtime_seconds": 60,
            "max_tool_calls": 1,
        },
    )
    return agent, provider


def prepare(fixtures, config, path):
    """Run one agent and pause a two-agent workflow at its real approval gate."""
    if config["max_model_calls"] != 3:
        raise ValueError("Live smoke is restricted to three planned model calls")
    t = fixtures["tenants"]["a"]
    solo, first_provider = configured_agent(t, config, config["solo_model"], "haiku")
    chained, second_provider = configured_agent(
        t, config, config["workflow_model"], "sonnet"
    )
    marker = "LIVE_CHECK_TENANCY_A"
    task = call(
        "POST",
        "/tasks",
        t["admin"],
        {
            "agent_id": solo["id"],
            "title": "Live Anthropic diagnostic",
            "initial_message": marker,
        },
    )
    wait_for("/tasks/" + task["task_id"], t["admin"], "latest_turn_status", "succeeded")
    plan = {
        "manual": True,
        "steps": [
            {
                "id": "draft",
                "type": "agent",
                "a": [chained["id"]],
                "t": "Draft a one-sentence response containing " + marker,
                "fail": "stop",
            },
            {
                "id": "approval",
                "type": "appr",
                "a": [],
                "t": "Approve the synthetic response",
                "fail": "stop",
            },
            {
                "id": "finish",
                "type": "agent",
                "a": [chained["id"]],
                "t": "Confirm the approved result and repeat " + marker,
                "fail": "stop",
            },
        ],
    }
    published = call(
        "POST",
        "/workflow-studio/publish",
        t["admin"],
        {"name": "Live Anthropic approval flow", "plan": plan},
    )
    wid = published["definition"]["workflow_id"]
    run = call("POST", f"/workflows/{wid}/runs", t["admin"], {"instructions": marker})
    run_path = f"/workflows/{wid}/runs/{run['run_id']}"
    paused = wait_for(run_path, t["admin"], "status", "awaiting_approval")
    if paused["states"][-1]["status"] != "queued":
        raise AssertionError(
            "Approval did not prevent the downstream agent from running"
        )
    fixtures["live"] = {
        "task_id": task["task_id"],
        "agent_id": solo["id"],
        "workflow_id": wid,
        "run_id": run["run_id"],
        "marker": marker,
        "provider_ids": [first_provider["id"], second_provider["id"]],
        "models": [config["solo_model"], config["workflow_model"]],
    }
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Live agent succeeded; workflow is waiting for browser approval.")


def verify(fixtures, config, path):
    """Verify model usage, pinned skills, tool provenance and cross-tenant denials."""
    t, other = fixtures["tenants"].values()
    live = fixtures["live"]
    resource = f"/workflows/{live['workflow_id']}/runs/{live['run_id']}"
    result = wait_for(resource, t["admin"], "status", "succeeded")
    if [s["status"] for s in result["states"]] != ["succeeded"] * 3:
        raise AssertionError("Live approval workflow did not complete every stage")
    tasks = [live["task_id"]] + [task for s in result["states"] for task in s["tasks"]]
    evidence = verify_records(tasks, t, live)
    for target in ("/agents/" + live["agent_id"], resource):
        denied = httpx.get(API + target, headers=other["admin"]["headers"], timeout=10)
        if denied.status_code != 404:
            raise AssertionError("Live data crossed tenant boundaries")
    listings = json.dumps(call("GET", "/model-providers", t["admin"]))
    if config["api_key"] in listings:
        raise AssertionError("Provider API exposed its credential")
    evidence.update(
        {
            "passed": True,
            "real_provider": "anthropic",
            "models": live["models"],
            "planned_model_calls": 3,
            "approval_blocked_then_completed": True,
            "foreign_tenant_denied": True,
            "key_redacted": True,
        }
    )
    (path.parent / "live-evidence.json").write_text(json.dumps(evidence, indent=2))
    print("Live model, skills, connector provenance and workflow evidence verified.")


def verify_records(task_ids, tenant, live):
    """Inspect durable model responses and usage without publishing their content."""
    calls = []
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        for task_id in task_ids:
            task = call("GET", "/tasks/" + task_id, tenant["admin"])
            turn = connection.execute(
                "SELECT payload FROM product_records WHERE "
                "collection='task_turns' AND org_id=%s AND payload->>'task_id'=%s",
                (tenant["org_id"], task_id),
            ).fetchone()[0]
            agent_run = connection.execute(
                "SELECT payload FROM product_records WHERE "
                "collection='agent_runs' AND org_id=%s AND record_id=%s",
                (tenant["org_id"], turn["agent_run_id"]),
            ).fetchone()[0]
            summary = agent_run.get("output_summary") or {}
            if task["latest_turn_status"] != "succeeded" or live[
                "marker"
            ] not in summary.get("response", ""):
                raise AssertionError(
                    "Live model did not produce the requested response"
                )
            if agent_run["resolved_provider_id"] not in live["provider_ids"]:
                raise AssertionError("Live model execution used an unexpected provider")
            if not summary.get("citations"):
                raise AssertionError("Live agent did not record connector citations")
            input_tokens, output_tokens = (
                agent_run.get("input_tokens"),
                agent_run.get("output_tokens"),
            )
            if not input_tokens or not output_tokens:
                raise AssertionError("Live provider usage was not recorded")
            version = connection.execute(
                "SELECT payload FROM product_records WHERE collection='agent_versions' "
                "AND org_id=%s AND record_id=%s",
                (tenant["org_id"], agent_run["agent_version_id"]),
            ).fetchone()[0]
            pins = [s.get("source_skill_version_id") for s in version["skills"]]
            if tenant["org_skill"]["version"]["skill_version_id"] not in pins:
                raise AssertionError(
                    "Live execution lost its pinned organization skill"
                )
            invocations = connection.execute(
                "SELECT payload FROM product_records "
                "WHERE collection='tool_invocations' "
                "AND org_id=%s AND payload->>'agent_run_id'=%s",
                (tenant["org_id"], agent_run["agent_run_id"]),
            ).fetchall()
            if len(invocations) != 1 or invocations[0][0]["status"] != "succeeded":
                raise AssertionError(
                    "Live read-only connector invocation was not successful"
                )
            calls.append(
                {
                    "model": agent_run["resolved_model_id"],
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "citations": len(summary["citations"]),
                    "skill_pinned": True,
                    "tool_invocations": 1,
                }
            )
    if len(calls) != 3:
        raise AssertionError("Live smoke exceeded or missed its three planned calls")
    return {"verified_model_calls": calls}


def main():
    """Require an isolated fixture and explicit private live credentials."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    config = json.loads((path.parent / "live-config.json").read_text())
    if sys.argv[1:] == ["prepare"]:
        prepare(fixtures, config, path)
    elif sys.argv[1:] == ["verify"]:
        verify(fixtures, config, path)
    else:
        raise ValueError("Choose prepare or verify; no unbounded live runner")


if __name__ == "__main__":
    main()
