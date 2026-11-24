"""Compatibility check and rollback rehearsal for a release candidate.

    python scripts/release_rehearsal.py run --output release-compat.json
    python scripts/release_rehearsal.py verify release-compat.json --revision <sha>

``run`` obtains the minimum supported rollback image (built from the
``min_rollback_revision`` in ``infra/gcp/singlevm/release_policy.json`` with
``git archive``, or ``--old-image`` when a local image already exists), builds
the candidate from this checkout (or ``--candidate-image``), then on a
disposable local Docker stack: seeds data with the old image, upgrades to the
candidate, rolls back to the old image on the newer schema, and rolls forward
again. It uses no cloud resource, registry or production credential, and writes
a manifest the deploy job verifies (fail closed) with ``verify``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rehearsal.manifest import load_policy, verify_manifest
from rehearsal.run import _git, build_from_revision, describe_image, rehearse
from rehearsal.stack import docker

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "infra/gcp/singlevm/release_policy.json"


def run_command(args: argparse.Namespace) -> int:
    """Rehearse and write the manifest; the exit status is the gate result."""
    policy = load_policy(POLICY)
    revision = _git(ROOT, "rev-parse", "HEAD")
    dirty = bool(_git(ROOT, "status", "--porcelain"))
    floor = policy["min_rollback_revision"]
    old_tag = args.old_image
    if old_tag is None:
        old_tag = f"weaves-rehearsal-floor:{floor[:12]}"
        build_from_revision(ROOT, floor, old_tag)
    new_tag = args.candidate_image
    if new_tag is None:
        new_tag = f"weaves-rehearsal-candidate:{revision[:12]}"
        docker("build", "-q", "-t", new_tag, str(ROOT))
    old = describe_image(old_tag, floor, "image" if args.old_image else "git-revision")
    new = describe_image(
        new_tag, revision, "image" if args.candidate_image else "checkout"
    )
    new["dirty"] = dirty
    manifest = rehearse(ROOT, old, new, old_tag, new_tag, args.scale)
    manifest["policy"] = {
        k: policy[k] for k in ("min_rollback_revision", "min_rollback_schema_version")
    }
    Path(args.output).write_text(json.dumps(manifest, indent=2) + "\n")
    for step in manifest["steps"]:
        print(
            ("ok   " if step["ok"] else "FAIL ")
            + step["name"]
            + ("  " + step["detail"] if step["detail"] else "")
        )
    print(f"rehearsal {manifest['result']} in {manifest['seconds']}s -> {args.output}")
    return 0 if manifest["result"] == "passed" else 1


def verify_command(args: argparse.Namespace) -> int:
    """Fail closed unless the manifest proves the rehearsal for this revision."""
    problems = verify_manifest(Path(args.manifest), POLICY, args.revision)
    for problem in problems:
        print("::error::release gate: " + problem)
    print("release gate: " + ("blocked" if problems else "passed"))
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    """Dispatch ``run`` or ``verify``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--output", default="release-compat.json")
    run.add_argument("--old-image", help="existing local image for the rollback floor")
    run.add_argument("--candidate-image", help="existing local image for the candidate")
    run.add_argument(
        "--scale", type=int, default=24, help="agents per tenant per phase"
    )
    check = commands.add_parser("verify")
    check.add_argument("manifest")
    check.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    return run_command(args) if args.command == "run" else verify_command(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
