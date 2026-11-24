r"""Operator-only: AI usage and estimated cost per organization.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - --by month < usage_report.py

Options:
  --by day|month         one row per organization and day or month (default
                         month)
  --from YYYY-MM-DD      first UTC day (default: the first day of this month)
  --to YYYY-MM-DD        last UTC day, inclusive (default: today)
  --org ORG_ID           one organization only
  --csv                  CSV on stdout instead of JSON

Columns: period, org_id, org_name, calls, input_tokens, output_tokens,
total_tokens, estimated_cost_usd, unpriced_calls (calls whose model has no
price, so their cost is missing), unknown_calls (requests that may have reached
the provider with no recorded token count). Cost is the model profile's
per-million rates applied to provider-reported tokens: customers use their own
provider keys, so this is what they spend, not a charge by Weaves. Usage is
removed together with its organization and kept for WEAVES_USAGE_RETENTION_DAYS
for live organizations.

Only counts and amounts are read: never prompts, outputs or credentials. The
script never seeds demo data and is not reachable over HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from modules.usage.operator_report import PERIODS, build_report, to_csv
from shared.clock import utc_now


def _date(text: str) -> date:
    return date.fromisoformat(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--by", choices=PERIODS, default="month")
    parser.add_argument("--from", dest="start", type=_date)
    parser.add_argument("--to", dest="end", type=_date)
    parser.add_argument("--org")
    parser.add_argument("--csv", action="store_true")
    return parser


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, print the report and return a process exit status."""
    args = _parser().parse_args(argv)
    today = utc_now().date()
    start = args.start or today.replace(day=1)
    end = args.end or today
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        rows = build_report(
            active.usage_ledger, start, end, by=args.by, org_id=args.org
        )
    except ValueError as exc:
        emit("operator.usage_report.refused", actor_kind="operator", reason=str(exc))
        return 2
    finally:
        if owned:
            active.close()
    emit(
        "operator.usage_report",
        actor_kind="operator",
        by=args.by,
        rows=len(rows),
        org_id=args.org,
    )
    if args.csv:
        sys.stdout.write(to_csv(rows))
    else:
        sys.stdout.write(json.dumps(rows, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
