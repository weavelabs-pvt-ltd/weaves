"""Disaster-recovery drill against a LOCAL throwaway Docker stack only.

    python scripts/dr_drill.py [--image IMAGE] [--output dr-drill.json] [--scale 4]

Builds the backend image from this checkout (or uses a local ``--image``),
starts its own PostgreSQL, API and worker on a private Docker network, seeds
synthetic tenants, backs up like ``backup.sh``, deletes a tenant, destroys the
database, recovers with the documented restore and tombstone-replay procedure
and checks the result. It reads no production setting, uses no cloud service and
refuses to start when the machine, Docker daemon, environment or image look like
production. The output file is evidence for the runbook and the private-beta gate.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rehearsal.drill import run_drill
from rehearsal.drill_safety import refusals
from rehearsal.stack import RehearsalError, docker

ROOT = Path(__file__).resolve().parents[1]


def _docker_endpoint() -> str:
    """The active Docker context's endpoint (never contacts it)."""
    result = docker(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def check_target(image: str | None) -> list[str]:
    """Every reason to refuse; an empty list means this is a local machine."""
    return refusals(
        os.environ,
        socket.gethostname(),
        _docker_endpoint(),
        lambda path: Path(path).exists(),
        image=image,
    )


def main(argv: list[str]) -> int:
    """Refuse unsafe targets, run the drill, write the evidence file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--image", help="existing local backend image to test")
    parser.add_argument("--output", default="dr-drill.json")
    parser.add_argument("--scale", type=int, default=4, help="agents per tenant")
    args = parser.parse_args(argv)
    problems = check_target(args.image)
    if problems:
        for problem in problems:
            print("refused: " + problem, file=sys.stderr)
        print("The drill only runs on a local throwaway stack.", file=sys.stderr)
        return 2
    image = args.image
    if image is None:
        image = "weaves-dr-drill:local"
        docker("build", "-q", "-t", image, str(ROOT))
    try:
        with tempfile.TemporaryDirectory(prefix="dr-drill-") as workdir:
            result = run_drill(image, Path(workdir), args.scale)
    except RehearsalError as error:
        print(f"drill aborted: {error}", file=sys.stderr)
        return 1
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    for step in result["steps"]:
        print(
            ("ok   " if step["ok"] else "FAIL ")
            + step["name"]
            + ("  " + step["detail"] if step["detail"] else "")
        )
    print(
        f"drill {result['result']} in {result['seconds']}s "
        f"(RTO {result['rto_seconds']}s, "
        f"backup-to-disaster {result['backup_to_disaster_seconds']}s) "
        f"-> {args.output}"
    )
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
