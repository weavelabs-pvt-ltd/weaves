r"""Operator-only: mark a database restore, then re-apply tombstones after it.

Run on the VM with the API and worker stopped, from the backend image so it can
read the tombstone directory and open the database as owner:

    docker compose run --rm --no-deps -T product-api python - \
        mark-restore --backup /opt/weaves/backups/singlevm/weaves-X.sql.gz \
        < restore_reconcile.py
    # ... restore the dump into the empty database ...
    docker compose run --rm --no-deps -T product-api python - \
        apply --head-file /opt/weaves/backups/singlevm/weaves-X.sql.gz.tombstone-head \
        < restore_reconcile.py

Subcommands:
  mark-restore  write restore-pending-<id>.json; the API, worker and the release
                script then refuse to start until ``apply`` succeeds.
  apply         replay the tombstone log (purges, credential revocations, secret
                deletions) onto the restored database, revoke all account
                sessions and write the completion marker. ``--dry-run`` prints
                the plan and changes nothing. It is idempotent: rerun after a
                crash. ``--head-file`` is the backup's ``.tombstone-head``
                sidecar; without it pass ``--no-head-check`` (rotated-secret
                tombstones are then skipped and listed in the report).
  status        list pending restores.

Set a NEW WEAVES_BROWSER_SESSION_SECRET in runtime.env before starting services.
Results are JSON on stdout; the tombstone log never contains secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from modules.recovery.reconcile import ReconcileRefused, reconcile
from modules.recovery.restore_gate import (
    mark_reconciled,
    mark_restore,
    pending_restores,
)
from modules.recovery.tombstones import FileTombstoneLog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=os.environ.get("WEAVES_TOMBSTONE_DIR", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    mark = commands.add_parser("mark-restore")
    mark.add_argument("--backup", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--restore-id")
    apply.add_argument("--head-file")
    apply.add_argument("--no-head-check", action="store_true")
    apply.add_argument("--dry-run", action="store_true")
    commands.add_parser("status")
    return parser


def _head(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.head_file:
        return dict(json.loads(Path(args.head_file).read_text()))
    if args.no_head_check:
        return None
    raise ReconcileRefused(
        "pass --head-file (the backup's .tombstone-head) or --no-head-check"
    )


def _restore_id(directory: Path, requested: str | None) -> str:
    pending = pending_restores(directory)
    if requested:
        return requested
    if len(pending) != 1:
        raise ReconcileRefused(f"expected one pending restore, found {pending}")
    return pending[0]


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Execute one subcommand; return a process exit status."""
    args = _parser().parse_args(argv)
    if not args.dir:
        sys.stderr.write("set WEAVES_TOMBSTONE_DIR or pass --dir\n")
        return 2
    directory = Path(args.dir)
    if args.command == "status":
        print(json.dumps({"pending": pending_restores(directory)}))
        return 0
    if args.command == "mark-restore":
        restore_id = mark_restore(directory, Path(args.backup).name)
        emit("operator.restore.marked", restore_id=restore_id)
        print(json.dumps({"restore_id": restore_id}))
        return 0
    owned = runtime is None
    try:
        head = _head(args)
        restore_id = _restore_id(directory, args.restore_id)
        active = open_owner_runtime() if runtime is None else runtime
        try:
            report = reconcile(
                active, FileTombstoneLog(directory), head=head, dry_run=args.dry_run
            )
        finally:
            if owned:
                active.close()
    except ReconcileRefused as exc:
        emit("operator.restore.refused", reason=str(exc))
        return 2
    if not args.dry_run:
        mark_reconciled(directory, restore_id, report)
        emit("operator.restore.reconciled", restore_id=restore_id)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
