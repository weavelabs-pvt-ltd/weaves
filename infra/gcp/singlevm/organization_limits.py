r"""Operator-only: set, clear and inspect one organization's limit overrides.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - \
        set --organization-id ORG --operator you@example.com \
        --reason "pilot customer" --seats 60 --max-queued 200 \
        < organization_limits.py

Subcommands: set (merges the given limits into the organization's override),
clear (back to the environment defaults), show and list. Every change needs
--operator and --reason, writes an ``organization.limits.override_*`` audit event
attributed to the operator and one structured, secret-free log line. Caps that
are not named keep their current value. An override takes effect within about
30 seconds (the API and worker cache it briefly). The process-wide switches
(WEAVES_LIMITS_MODE, WEAVES_ADMISSIONS_PAUSED, the global queue cap) stay
environment settings. Results are printed as JSON on stdout. This script is not
reachable over HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from modules.limits.overrides import OrganizationLimitOverrides

# CLI flag -> OrgLimits field
FLAGS: dict[str, str] = {
    "max-queued": "max_queued_per_org",
    "requests-per-minute": "requests_per_minute_org",
    "requests-per-minute-per-principal": "requests_per_minute_principal",
    "request-burst": "request_burst",
    "seats": "seats",
    "schedules": "schedules",
    "agents": "agents",
    "workflows": "workflows",
    "tool-calls-per-day": "tool_calls_per_day",
    "storage-tasks": "storage_tasks",
    "tokens-per-day": "tokens_per_day",
    "tokens-per-month": "tokens_per_month",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("set")
    clear = commands.add_parser("clear")
    for sub in (change, clear):
        sub.add_argument("--organization-id", required=True)
        sub.add_argument("--operator", required=True)
        sub.add_argument("--reason", required=True)
    for flag in FLAGS:
        change.add_argument(f"--{flag}", type=int)
    show = commands.add_parser("show")
    show.add_argument("--organization-id", required=True)
    commands.add_parser("list")
    return parser


def _service(runtime: Any) -> OrganizationLimitOverrides:
    return OrganizationLimitOverrides(
        overrides=runtime.organization_limits,
        organizations=runtime.organizations,
        audit_events=runtime.audit_events,
        defaults=runtime.admission.policy.base,
        unit_of_work=runtime._unit_of_work,
    )


def execute(runtime: Any, args: argparse.Namespace) -> Any:
    """Run one subcommand against a runtime and return its JSON-able result."""
    service = _service(runtime)
    if args.command == "list":
        return service.list()
    if args.command == "show":
        return service.show(args.organization_id)
    if args.command == "clear":
        return service.clear(args.organization_id, args.operator, args.reason)
    values = {
        field: getattr(args, flag.replace("-", "_"))
        for flag, field in FLAGS.items()
        if getattr(args, flag.replace("-", "_")) is not None
    }
    return service.set(args.organization_id, args.operator, args.reason, values)


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, execute and return a process exit status."""
    args = _parser().parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        result = execute(active, args)
    except (ValueError, PermissionError, KeyError) as exc:
        emit(
            "operator.organization_limits.refused",
            command=args.command,
            reason=str(exc),
        )
        return 2
    finally:
        if owned:
            active.close()
    if args.command in {"set", "clear"}:
        emit(
            "operator.organization_limits",
            command=args.command,
            organization_id=args.organization_id,
            operator=args.operator,
        )
    sys.stdout.write(json.dumps(result, default=str, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
