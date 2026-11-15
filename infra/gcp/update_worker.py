"""Update an existing Cloud Run worker using the v2 API and gcloud identity."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from typing import Any


def main() -> None:
    """Preserve worker configuration while replacing its image and pool limit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    token = subprocess.check_output(
        [
            "gcloud",
            "auth",
            "print-access-token",
            f"--account={args.account}",
            "--quiet",
        ],
        text=True,
    ).strip()

    def request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        call = urllib.request.Request(
            f"https://run.googleapis.com/v2/{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="PATCH" if payload is not None else "GET",
        )
        with urllib.request.urlopen(call, timeout=60) as response:
            return json.load(response)

    name = f"projects/{args.project}/locations/{args.region}/workerPools/weaves-worker"
    worker = request(name)
    containers = worker["template"]["containers"]
    containers[0]["image"] = args.image
    containers[0]["command"] = ["python"]
    containers[0]["args"] = ["-m", "apps.worker.main"]
    env = [
        entry
        for entry in containers[0].get("env", [])
        if entry["name"] != "WEAVES_DB_POOL_MAX_SIZE"
    ]
    containers[0]["env"] = [
        *env,
        {"name": "WEAVES_DB_POOL_MAX_SIZE", "value": "4"},
    ]
    operation = request(
        f"{name}?updateMask=template.containers",
        {
            "name": name,
            "etag": worker["etag"],
            "template": {"containers": containers},
        },
    )
    print("Worker update accepted; waiting for readiness.", flush=True)
    deadline = time.monotonic() + 600
    while not operation.get("done"):
        if time.monotonic() >= deadline:
            raise TimeoutError("Worker update did not complete within ten minutes")
        time.sleep(5)
        operation = request(operation["name"])
    if operation.get("error"):
        raise RuntimeError(f"Worker update failed: {operation['error']}")
    ready = request(name)
    if ready.get("terminalCondition", {}).get("state") != "CONDITION_SUCCEEDED":
        raise RuntimeError("Worker did not reach a ready state")
    print(f"Worker ready: {ready['latestReadyRevision']}")


if __name__ == "__main__":
    main()
