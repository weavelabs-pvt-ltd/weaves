"""Read-only observer for durable results from the independent native worker."""

import json
import os
import time
from pathlib import Path

import psycopg

from tenancy_harness.model_egress import observe_model_egress
from tenancy_harness.seed import require_disposable_database


def main():
    """Record known lifecycle behavior without a process-turn testing endpoint."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    lifecycle = fixtures["diagnostics"]["lifecycle"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
            schema = connection.execute(
                "SELECT max(version) FROM product_schema_migrations"
            ).fetchone()[0]
            row = connection.execute(
                "SELECT payload FROM product_records "
                "WHERE collection = 'tasks' AND record_id = %s AND org_id = %s",
                (lifecycle["task_id"], lifecycle["org_id"]),
            ).fetchone()
        if row and row[0].get("latest_turn_status") in {"succeeded", "failed"}:
            lifecycle["observed_turn_status"] = row[0]["latest_turn_status"]
            fixtures["schema_version"] = schema
            fixtures["diagnostics"]["T0-D03"] = (
                lifecycle["api_denied"]
                and lifecycle["webhook_accepted_after_suspension"]
                and row[0]["latest_turn_status"] == "succeeded"
            )
            observe_model_egress(fixtures)
            path.write_text(json.dumps(fixtures))
            path.chmod(0o600)
            print("Independent worker durable outcome recorded.")
            return
        time.sleep(0.5)
    raise RuntimeError("Native worker did not finish its durable lifecycle fixture")


if __name__ == "__main__":
    main()
