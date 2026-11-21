r"""Operator-only: record Asia/Kolkata on legacy schedules and calendar settings.

Production correctness never depends on this script: records without a
``timezone`` are read as Asia/Kolkata by the shared occurrence module. Running it
only makes that interpretation explicit in storage. It opens the database
owner's connection and is stream-run like the other operator scripts:

    docker compose exec -T product-api python - --apply \
        < backfill_schedule_timezones.py

The default is a dry run that only reports counts. ``--apply`` writes
``timezone='Asia/Kolkata'`` into interval schedules and calendar settings that
have none. Records that already name a zone are never touched, ``updated_at``,
``next_run_at`` and every other field are preserved, and re-running changes
nothing. It prints only counts and identifiers, never record contents.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from modules.workflows.schedule_time import LEGACY_TIMEZONE
from shared.request_context import system_scope

COLLECTIONS = ("workflow_schedules", "canvas_settings")


def backfill(runtime: Any, *, apply: bool) -> dict[str, dict[str, int]]:
    """Report (and with ``apply`` write) legacy records lacking a timezone."""
    summary: dict[str, dict[str, int]] = {}
    with system_scope("operator"):
        for name in COLLECTIONS:
            repository = getattr(runtime, name)
            legacy = [r for r in repository.list() if r.timezone is None]
            total = len(repository.list())
            if apply:
                for record in legacy:
                    repository.put(
                        record.model_copy(update={"timezone": LEGACY_TIMEZONE})
                    )
            summary[name] = {
                "total": total,
                "legacy": len(legacy),
                "updated": len(legacy) if apply else 0,
            }
    return summary


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, run the backfill and return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        emit("operator.backfill_schedule_timezones.refused", reason="exclusive flags")
        return 2
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        summary = backfill(active, apply=args.apply)
    finally:
        if owned:
            active.close()
    emit(
        "operator.backfill_schedule_timezones",
        mode="apply" if args.apply else "dry-run",
        zone=LEGACY_TIMEZONE,
        **summary,
    )
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
