r"""Operator-only: suspend, resume and delete organizations.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - \
        suspend --organization-id ORG --operator you@example.com \
        --reason "billing hold" < organization_lifecycle.py

Subcommands: suspend, resume, request-deletion, cancel-deletion, purge-now
(needs --confirm-organization-name), status and list. Every action requires
--operator and --reason, writes an ``organization.lifecycle.*`` audit event
attributed to the operator and one structured, secret-free log line. A
suspension can only be undone here: the organization owner cannot resume it.
Deletion starts a grace period (WEAVES_ORGANIZATION_DELETION_GRACE_DAYS); the
worker then purges the organization. Results are printed as JSON on stdout.
This script is not reachable over HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime

ACTIONS = ("suspend", "resume", "request-deletion", "cancel-deletion", "purge-now")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ACTIONS:
        sub = commands.add_parser(name)
        sub.add_argument("--organization-id", required=True)
        sub.add_argument("--operator", required=True)
        sub.add_argument("--reason", required=True)
        if name == "purge-now":
            sub.add_argument("--confirm-organization-name", required=True)
    status = commands.add_parser("status")
    status.add_argument("--organization-id", required=True)
    commands.add_parser("list")
    return parser


def execute(runtime: Any, args: argparse.Namespace) -> Any:
    """Run one subcommand against a runtime and return its JSON-able result."""
    service = runtime.organization_lifecycle_service
    if args.command == "list":
        return service.operator_list()
    if args.command == "status":
        result = service.operator_status(args.organization_id)
        result["remaining_rows"] = runtime.organization_purge_service.remaining_rows(
            args.organization_id
        )
        return result
    org, who, why = args.organization_id, args.operator, args.reason
    if args.command == "suspend":
        return service.suspend(org, who, why)
    if args.command == "resume":
        return service.resume(org, who, why)
    if args.command == "request-deletion":
        return service.operator_request_deletion(org, who, why)
    if args.command == "cancel-deletion":
        return service.operator_cancel_deletion(org, who, why)
    return service.purge_now(org, who, why, args.confirm_organization_name)


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, execute and return a process exit status."""
    args = _parser().parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        result = execute(active, args)
    except (ValueError, PermissionError, KeyError) as exc:
        emit(
            "operator.organization_lifecycle.refused",
            command=args.command,
            reason=str(exc),
        )
        return 2
    finally:
        if owned:
            active.close()
    if args.command not in {"status", "list"}:
        emit(
            "operator.organization_lifecycle",
            command=args.command,
            organization_id=args.organization_id,
            operator=args.operator,
            state=result.get("state"),
        )
    sys.stdout.write(json.dumps(result, default=str, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
