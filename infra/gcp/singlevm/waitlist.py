r"""Operator-only: read, export, report on and erase waitlist sign-ups.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - list --status new \
        < waitlist.py

Subcommands:
  list [--status S] [--domain-type company|public] [--since D] [--until D]
       [--answer QUESTION=OPTION]   entries with their latest answers (JSON)
  export --csv --operator WHO [same filters]   a CSV on stdout
  stats                                      sign-ups per day, completion per
                                             question, answer distribution,
                                             company versus public share
  set-status --email E --status new|reviewed|contacted|converted --operator WHO
  note --email E --text "..." --operator WHO  (empty text clears the note)
  delete --email E --operator WHO            erase the entry and every
                                             submission (right to erasure)

Every action writes one structured, secret-free log line with
``actor_kind=operator``; status and note changes also append to the entry's own
history. Resume tokens are stored only as digests and are never printed. The
script never seeds demo data (it refuses WEAVES_SEED_LOCAL_DEFAULTS=true) and
is not reachable over HTTP. Waitlist rows are never part of any organization.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from contracts.v1 import WaitlistDomainType, WaitlistStatus
from modules.waitlist.admin import (
    Filters,
    WaitlistAdmin,
    parse_answer_filter,
    parse_date,
)
from modules.waitlist.errors import InvalidInput

MUTATING = ("set-status", "note", "delete", "export")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "export", "stats", "set-status", "note", "delete"):
        sub = commands.add_parser(name)
        sub.add_argument("--operator", required=name in MUTATING)
        if name in ("list", "export"):
            sub.add_argument("--status", choices=[s.value for s in WaitlistStatus])
            sub.add_argument(
                "--domain-type", choices=[d.value for d in WaitlistDomainType]
            )
            sub.add_argument("--since")
            sub.add_argument("--until")
            sub.add_argument("--answer")
        if name == "export":
            sub.add_argument("--csv", action="store_true", required=True)
        if name in ("set-status", "note", "delete"):
            sub.add_argument("--email", required=True)
        if name == "set-status":
            sub.add_argument("--status", required=True)
        if name == "note":
            sub.add_argument("--text", required=True)
    return parser


def _filters(args: argparse.Namespace) -> Filters:
    return Filters(
        status=WaitlistStatus(args.status) if args.status else None,
        domain_type=WaitlistDomainType(args.domain_type) if args.domain_type else None,
        since=parse_date(args.since),
        until=parse_date(args.until),
        answer=parse_answer_filter(args.answer) if args.answer else None,
    )


def execute(admin: WaitlistAdmin, args: argparse.Namespace) -> tuple[Any, bool]:
    """Run one subcommand; return (result, result is raw text)."""
    command = args.command
    if command == "list":
        return admin.list_entries(_filters(args)), False
    if command == "export":
        return admin.export_csv(_filters(args)), True
    if command == "stats":
        return admin.stats(), False
    if command == "set-status":
        return admin.set_status(args.email, args.status, args.operator), False
    if command == "note":
        return admin.note(args.email, args.text, args.operator), False
    return admin.delete(args.email), False


def _count(result: Any, raw: bool) -> int | None:
    if raw:
        return max(0, result.count("\n") - 1)
    return len(result) if isinstance(result, list) else None


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, execute and return a process exit status."""
    args = _parser().parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        result, raw = execute(WaitlistAdmin(active.waitlist_service), args)
    except (InvalidInput, KeyError, ValueError) as exc:
        emit(
            "operator.waitlist.refused",
            actor_kind="operator",
            command=args.command,
            reason=str(exc).strip("'"),
        )
        return 2
    finally:
        if owned:
            active.close()
    fields = {"entry_id": result.get("entry_id")} if isinstance(result, dict) else {}
    emit(
        "operator.waitlist",
        actor_kind="operator",
        command=args.command,
        operator=args.operator,
        rows=_count(result, raw),
        **fields,
    )
    if raw:
        sys.stdout.write(result)
    else:
        sys.stdout.write(json.dumps(result, default=str, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
