"""Operator command: list recorded releases and roll back to a ledger digest.

Run on the VM as root. Rollback refuses any backend release older than the
minimum rollback revision in ``release_policy.json``. The registry token is read
from stdin, exactly like ``apply_release.py``.

    sudo python3 rollback_release.py list [--component backend]
    gcloud auth print-access-token | sudo python3 rollback_release.py rollback \
        --component backend --to SEQ_OR_DIGEST [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apply_release import deploy
from release_ledger import (
    POLICY,
    RollbackRefused,
    check_rollback,
    find_target,
    format_entries,
    load_policy,
    read_ledger,
    rollback_floor,
)
from release_runtime import release_lock

ROOT = Path("/opt/weaves/singlevm")


def list_releases(root: Path, component: str | None) -> str:
    """Show the ledger with the rollback floor marked."""
    entries = read_ledger(root)
    try:
        floor = rollback_floor(entries, load_policy(root / POLICY))
    except RollbackRefused:
        floor = None
    shown = [e for e in entries if component in (None, e["component"])]
    return format_entries(shown, floor)


def rollback(
    root: Path, component: str, reference: str, token: str, dry_run: bool
) -> str:
    """Validate the target against the policy, then redeploy its digest."""
    with release_lock(root):
        entries = read_ledger(root)
        target = find_target(entries, component, reference)
        check_rollback(entries, load_policy(root / POLICY), target)
        current = [e for e in entries if e["component"] == component][-1]
        if current["image"] == target["image"] and current["result"] == "healthy":
            raise RollbackRefused("That release is already running")
    message = f"{component} rollback to seq {target['seq']}: {target['image']}"
    if dry_run:
        return "Dry run, would apply " + message
    deploy(
        root, component, target["image"], target["revision"], token, None, "rollback"
    )
    return "Applied " + message


def main() -> None:
    """Parse the operator command and print the outcome."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="show every recorded release")
    listing.add_argument("--component", choices=("backend", "frontend"))
    back = commands.add_parser("rollback", help="redeploy a ledger digest")
    back.add_argument("--component", required=True, choices=("backend", "frontend"))
    back.add_argument("--to", required=True, help="ledger seq or image digest")
    back.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "list":
            print(list_releases(ROOT, args.component))
        else:
            token = "" if args.dry_run else sys.stdin.read().strip()
            print(rollback(ROOT, args.component, args.to, token, args.dry_run))
    except RollbackRefused as error:
        sys.exit(f"Refused: {error}")


if __name__ == "__main__":
    main()
