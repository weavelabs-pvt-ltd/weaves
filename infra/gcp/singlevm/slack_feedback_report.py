r"""Operator-only: Slack answer feedback (thumbs up and down) for the analytics.

Run on the VM with database owner access (it opens the owner connection), for
example from the backend image:

    docker compose exec -T product-api python - --by day < slack_feedback_report.py
    docker compose exec -T product-api python - --request-id slack-Ev0123 \
        < slack_feedback_report.py

Counts (default): one row per period, organization, agent, model and connector
set with ``up``, ``down`` and ``total`` (a connector set is the sorted list of
capability ids the answer was allowed to use; empty means general knowledge).
Options:
  --by day|month         UTC period (default month)
  --from YYYY-MM-DD      first UTC day (default: the first day of this month)
  --to YYYY-MM-DD        last UTC day, inclusive (default: today)
  --org ORG_ID           one organization only
  --csv                  CSV on stdout instead of JSON

Lookup: ``--request-id`` returns the ratings of one request (the request id of a
Slack answer is ``slack-{event_id}``) with the organization, task, turn, agent
run, value and time, so a complaint can be followed to the run and the audit
trail. Neither mode returns an asker (a member id or a keyed hash), a Slack user
id, message text or a reason: Slack reactions carry none and none is stored.
Feedback is kept WEAVES_USAGE_RETENTION_DAYS days (default 400) and deleted with
its organization. The script never seeds demo data and is not reachable over HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from apps.cli.operator_runtime import emit, open_owner_runtime
from modules.slack.feedback.report import (
    COLUMNS,
    LOOKUP_COLUMNS,
    PERIODS,
    lookup,
    summary,
    to_csv,
)
from shared.clock import utc_now


def _date(text: str) -> date:
    return date.fromisoformat(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--by", choices=PERIODS, default="month")
    parser.add_argument("--from", dest="start", type=_date)
    parser.add_argument("--to", dest="end", type=_date)
    parser.add_argument("--org")
    parser.add_argument("--request-id")
    parser.add_argument("--csv", action="store_true")
    return parser


def _rows(args: argparse.Namespace, admin: Any) -> list[dict[str, Any]]:
    if args.request_id:
        return lookup(admin, args.request_id)
    today = utc_now().date()
    start = args.start or today.replace(day=1)
    return summary(admin, start, args.end or today, by=args.by, org_id=args.org)


def run(argv: list[str], runtime: Any | None = None) -> int:
    """Parse arguments, print the report and return a process exit status."""
    args = _parser().parse_args(argv)
    owned = runtime is None
    active = open_owner_runtime() if runtime is None else runtime
    try:
        rows = _rows(args, active.slack_feedback_admin)
    except ValueError as exc:
        emit(
            "operator.slack_feedback_report.refused",
            actor_kind="operator",
            reason=str(exc),
        )
        return 2
    finally:
        if owned:
            active.close()
    emit(
        "operator.slack_feedback_report",
        actor_kind="operator",
        mode="lookup" if args.request_id else "counts",
        rows=len(rows),
        org_id=args.org,
    )
    if args.csv:
        columns = LOOKUP_COLUMNS if args.request_id else COLUMNS
        sys.stdout.write(to_csv(rows, columns))
    else:
        sys.stdout.write(json.dumps(rows, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
