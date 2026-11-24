r"""Operator-only: suspend, resume and delete organizations.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - \
        suspend --organization-id ORG --operator you@example.com \
        --reason "billing hold" < organization_lifecycle.py

Subcommands: suspend, resume, request-deletion, cancel-deletion, purge-now
(needs --confirm-organization-name), quarantine, release-quarantine,
quarantine-status, status and list. Every action requires --operator and
--reason (--ticket is optional), writes an ``organization.lifecycle.*`` audit
event attributed to the operator, one append-only ``operator_actions`` row (also
for a refused attempt) and one structured, secret-free log line carrying the
row's ``correlation_id``.

``quarantine`` is the emergency stop for one tenant: it suspends the
organization (API credentials and sessions stop working, workers cancel its work
at the next boundary), disables its active schedules and webhook triggers and
records exactly what it changed. ``--revoke-credentials`` additionally revokes
its API credentials, which is irreversible (it writes tombstones).
``release-quarantine`` re-enables only what quarantine disabled and resumes the
organization only if quarantine suspended it; revoked credentials stay revoked.
Other organizations are never touched. A
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

from apps.cli.operator_runtime import emit, open_owner_runtime, record_action
from modules.platform_controls.audit import new_correlation_id
from modules.platform_controls.quarantine import QuarantineService
from modules.recovery.tombstones import tombstones_from_env

ACTIONS = (
    "suspend",
    "resume",
    "request-deletion",
    "cancel-deletion",
    "purge-now",
    "quarantine",
    "release-quarantine",
)
QUARANTINE_COMMANDS = {"quarantine", "release-quarantine", "quarantine-status"}
# Quarantine records its own (atomic) audit row; the others get one in ``_run``.
SELF_AUDITED = {"quarantine", "release-quarantine"}
READ_ONLY = {"status", "list", "quarantine-status"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ACTIONS:
        sub = commands.add_parser(name)
        sub.add_argument("--organization-id", required=True)
        sub.add_argument("--operator", required=True)
        sub.add_argument("--reason", required=True)
        sub.add_argument("--ticket")
        if name == "purge-now":
            sub.add_argument("--confirm-organization-name", required=True)
        if name == "quarantine":
            sub.add_argument("--revoke-credentials", action="store_true")
    for name in ("status", "quarantine-status"):
        status = commands.add_parser(name)
        status.add_argument("--organization-id", required=True)
    commands.add_parser("list")
    return parser


def _quarantine(runtime: Any) -> QuarantineService:
    return QuarantineService(
        store=runtime.controls_store,
        audit=runtime.operator_audit,
        lifecycle=runtime.organization_lifecycle_service,
        schedules=runtime.workflow_schedules,
        triggers=runtime.workflow_triggers,
        credentials=runtime.api_credentials,
        tombstones=tombstones_from_env(),
        unit_of_work=runtime._unit_of_work,
    )


def _execute_quarantine(runtime: Any, args: argparse.Namespace, cid: str) -> Any:
    service = _quarantine(runtime)
    if args.command == "quarantine-status":
        return service.status(args.organization_id)
    if args.command == "quarantine":
        return service.quarantine(
            args.organization_id,
            args.operator,
            args.reason,
            revoke_credentials=args.revoke_credentials,
            ticket=args.ticket,
            correlation_id=cid,
        )
    return service.release(
        args.organization_id,
        args.operator,
        args.reason,
        ticket=args.ticket,
        correlation_id=cid,
    )


def execute(
    runtime: Any, args: argparse.Namespace, correlation_id: str | None = None
) -> Any:
    """Run one subcommand against a runtime and return its JSON-able result."""
    service = runtime.organization_lifecycle_service
    if args.command in QUARANTINE_COMMANDS:
        return _execute_quarantine(
            runtime, args, correlation_id or new_correlation_id()
        )
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
        return _run(active, args, new_correlation_id())
    finally:
        if owned:
            active.close()


def _refused(active: Any, args: argparse.Namespace, cid: str, exc: Exception) -> int:
    emit(
        "operator.organization_lifecycle.refused",
        command=args.command,
        reason=str(exc),
        correlation_id=cid,
    )
    if args.command not in READ_ONLY:
        record_action(
            active,
            operator=args.operator,
            command=f"organization.{args.command}",
            reason=args.reason,
            target=args.organization_id,
            org_id=args.organization_id,
            result="refused",
            details={"error": str(exc)[:200]},
            ticket=args.ticket,
            correlation_id=cid,
        )
    return 2


def _run(active: Any, args: argparse.Namespace, cid: str) -> int:
    mutating = args.command not in READ_ONLY
    try:
        result = execute(active, args, cid)
    except (ValueError, PermissionError, KeyError) as exc:
        return _refused(active, args, cid, exc)
    audited = True
    if mutating and args.command not in SELF_AUDITED:
        audited = record_action(
            active,
            operator=args.operator,
            command=f"organization.{args.command}",
            reason=args.reason,
            target=args.organization_id,
            org_id=args.organization_id,
            details={"state": result.get("state")},
            ticket=args.ticket,
            correlation_id=cid,
        )
    if mutating:
        emit(
            "operator.organization_lifecycle",
            command=args.command,
            organization_id=args.organization_id,
            operator=args.operator,
            state=result.get("state"),
            correlation_id=cid,
        )
    sys.stdout.write(json.dumps(result, default=str, sort_keys=True) + "\n")
    return 0 if audited else 3


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
