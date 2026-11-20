"""Prepare an approval flow with fake models before spending on a live model."""

import json
import os
import sys
from pathlib import Path

from tenancy_harness.client import call
from tenancy_harness.live import wait_for
from tenancy_harness.live_records import capture_records, verify_records
from tenancy_harness.seed import require_disposable_database


def main():
    """Check every workflow inspection API using actual PostgreSQL-backed resources."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    t = fixtures["tenants"]["a"]
    if sys.argv[1:] == ["verify"]:
        return verify(fixtures, t, path)
    profiles = call("GET", "/model-profiles", t["admin"])
    profile = next(p for p in profiles if p["provider_id"] == t["provider_id"])
    agent = call(
        "POST",
        "/agents",
        t["admin"],
        {
            "name": "fake-migrated-tool-probe",
            "instructions": t["marker"],
            "model_profile_id": profile["id"],
            "allowed_capability_ids": t["installation"]["enabled_capability_ids"],
            "skill_version_ids": [t["org_skill"]["version"]["skill_version_id"]],
            "max_tool_calls": 1,
        },
    )
    aid = agent["id"]
    task = call(
        "POST",
        "/tasks",
        t["admin"],
        {
            "agent_id": aid,
            "title": "Fake migrated tool probe",
            "initial_message": t["marker"],
        },
    )
    wait_for("/tasks/" + task["task_id"], t["admin"], "latest_turn_status", "succeeded")
    steps = [
        {"id": "draft", "type": "agent", "a": [aid], "t": t["marker"], "fail": "stop"},
        {
            "id": "approval",
            "type": "appr",
            "a": [],
            "t": "Approve fixture",
            "fail": "stop",
        },
        {"id": "finish", "type": "agent", "a": [aid], "t": t["marker"], "fail": "stop"},
    ]
    result = call(
        "POST",
        "/workflow-studio/publish",
        t["admin"],
        {"name": "Canvas inspection fixture", "plan": {"steps": steps}},
    )
    wid = result["definition"]["workflow_id"]
    for suffix in ("", "/studio/settings", "/runs"):
        call("GET", "/workflows/" + wid + suffix, t["admin"])
    run = call(
        "POST",
        "/workflows/" + wid + "/runs",
        t["admin"],
        {"instructions": "LIVE_CHECK_TENANCY_A"},
    )
    resource = f"/workflows/{wid}/runs/{run['run_id']}"
    wait_for(resource, t["admin"], "status", "awaiting_approval")
    fixtures["live"] = {
        "task_id": task["task_id"],
        "workflow_id": wid,
        "run_id": run["run_id"],
        "marker": t["marker"],
        "provider_ids": [t["provider_id"]],
    }
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Fake-model canvas inspection APIs verified; approval is waiting.")


def verify(fixtures, tenant, path):
    """Independently verify fake-model skills/tool provenance before paid testing."""
    live = fixtures["live"]
    resource = f"/workflows/{live['workflow_id']}/runs/{live['run_id']}"
    run = wait_for(resource, tenant["admin"], "status", "succeeded")
    tasks = [live["task_id"]] + [
        task for step in run["states"] for task in step["tasks"]
    ]
    records = capture_records(tasks, tenant, path.parent / "fake-live-records.json")
    verify_records(records, live, tenant["org_skill"]["version"]["skill_version_id"])
    print(
        "Fake model, migrated connector, pinned skill and approval provenance verified."
    )


if __name__ == "__main__":
    main()
