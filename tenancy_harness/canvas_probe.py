"""Prepare an approval flow with fake models before spending on a live model."""

import json
import os
from pathlib import Path

from tenancy_harness.client import call
from tenancy_harness.live import wait_for
from tenancy_harness.seed import require_disposable_database


def main():
    """Check every workflow inspection API using actual PostgreSQL-backed resources."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    t = fixtures["tenants"]["a"]
    aid = t["agent"]["id"]
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
        "task_id": t["task"]["task_id"],
        "workflow_id": wid,
        "run_id": run["run_id"],
        "marker": t["marker"],
    }
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    print("Fake-model canvas inspection APIs verified; approval is waiting.")


if __name__ == "__main__":
    main()
