r"""Operator-only: platform kill switches, read at runtime without a restart.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - \
        set --operator you@example.com --reason "incident 42" \
        --admissions-paused true < platform_control.py

Subcommands:
  status                       stored overrides and the effective values
  set    --operator --reason   force one or more switches (see below)
  clear  --operator --reason   return switches to their environment default
  actions [--limit N] [--organization-id ORG]   recent operator audit rows

Switches (all default to "not set", which leaves the environment defaults in
force, so nothing changes until an operator sets one):
  --admissions-paused true|false   refuse new queued work (429 ``limit_exceeded``
                                   resource ``admissions_paused``) in every limits
                                   mode; unset follows WEAVES_ADMISSIONS_PAUSED
  --worker-paused true|false       workers stop claiming new jobs and turns
                                   (running work finishes); queued work waits
  --read-only true|false           the API refuses every write with HTTP 503 and
                                   code ``platform_read_only``; reads still work
  --signup-enabled true|false      false refuses creating new accounts through
                                   Google sign-in (existing accounts sign in)
  --message TEXT                   shown to callers in the read-only 503

``clear`` with no --switch clears every switch and the message. Each processes
re-reads the row about every 5 seconds, so a change is live everywhere within
about 5 seconds (plus the worker poll interval). Every set/clear writes one
append-only ``operator_actions`` row in the same transaction as the change, and
one structured, secret-free log line. Not reachable over HTTP. Results are JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime, record_action
from modules.platform_controls.admin import ControlsAdmin
from modules.platform_controls.audit import new_correlation_id
from modules.platform_controls.controls import SWITCHES

FLAG_FOR: dict[str, str] = {name: name.replace("_", "-") for name in SWITCHES}


def _boolean(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    raise argparse.ArgumentTypeError("use true or false")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("set")
    clear = commands.add_parser("clear")
    for sub in (change, clear):
        sub.add_argument("--operator", required=True)
        sub.add_argument("--reason", required=True)
        sub.add_argument("--ticket")
    for name, flag in FLAG_FOR.items():
        change.add_argument(f"--{flag}", type=_boolean, dest=name)
    change.add_argument("--message")
    clear.add_argument("--switch", action="append", choices=list(SWITCHES), default=[])
    clear.add_argument("--clear-message", action="store_true")
    commands.add_parser("status")
    actions = commands.add_parser("actions")
    actions.add_argument("--limit", type=int, default=20)
    actions.add_argument("--organization-id")
    return parser


def execute(
    runtime: Any, args: argparse.Namespace, correlation_id: str | None = None
) -> Any:
    """Run one subcommand against a runtime and return its JSON-able result."""
    admin = ControlsAdmin(
        runtime.controls_store, runtime.operator_audit, runtime.platform_controls
    )
    if args.command == "status":
        return admin.status()
    if args.command == "actions":
        return [
            {
                "at": action.at.isoformat(),
                "operator": action.operator,
                "command": action.command,
                "target": action.target,
                "reason": action.reason,
                "ticket": action.ticket,
                "result": action.result,
                "correlation_id": action.correlation_id,
                "details": action.details,
            }
            for action in runtime.operator_audit.recent(
                args.limit, args.organization_id
            )
        ]
    cid = correlation_id or new_correlation_id()
    if args.command == "clear":
        return admin.clear(
            args.operator,
            args.reason,
            tuple(args.switch),
            clear_message=args.clear_message,
            ticket=args.ticket,
            correlation_id=cid,
        )
    values = {
        name: getattr(args, name)
        for name in SWITCHES
        if getattr(args, name) is not None
    }
    return admin.set(
        args.operator,
        args.reason,
        values,
        message=args.message,
        ticket=args.ticket,
        correlation_id=cid,
    )


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, execute and return a process exit status."""
    args = _parser().parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    cid = new_correlation_id()
    try:
        result = execute(active, args, cid)
    except (ValueError, PermissionError) as exc:
        emit(
            "operator.platform_control.refused",
            command=args.command,
            reason=str(exc),
            correlation_id=cid,
        )
        if args.command in {"set", "clear"}:
            record_action(
                active,
                operator=args.operator,
                command=f"platform.{args.command}",
                reason=args.reason,
                target="platform_controls",
                result="refused",
                details={"error": str(exc)[:200]},
                ticket=args.ticket,
                correlation_id=cid,
            )
        return 2
    finally:
        if owned:
            active.close()
    if args.command in {"set", "clear"}:
        emit(
            "operator.platform_control",
            command=args.command,
            operator=args.operator,
            version=result["version"],
            correlation_id=cid,
        )
    sys.stdout.write(json.dumps(result, default=str, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
