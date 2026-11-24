"""Private-beta release gate: a read-only report, never an approval.

    python scripts/private_beta_gate.py [--evidence-dir DIR]
        [--production-state state.json] [--github] [--json]

Each item prints ``pass``, ``fail``, ``blocked-on-owner`` (an owner decision or
approval is outstanding) or ``unverified`` (production evidence was not given).
Repository items read this checkout and the evidence files in ``--evidence-dir``
(``dr-drill.json`` from ``scripts/dr_drill.py`` and ``release-compat.json`` from
``scripts/release_rehearsal.py`` or the CI artifact). Production items read only
the JSON an operator collected with ``scripts/gate_collect_production.py``; the
gate itself never connects to production, never writes, and never changes
configuration. ``--github`` makes one read-only GitHub API request. Public
signup stays off whatever this prints: inviting anyone is a human decision.

Exit status: 0 every item passes, 1 at least one fails, 2 nothing fails but some
item is blocked on the owner or unverified.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from beta_gate.model import (
    BLOCKED,
    FAIL,
    PASS,
    STATUSES,
    UNVERIFIED,
    Context,
    Result,
    load_json,
)
from beta_gate.production_checks import PRODUCTION_CHECKS
from beta_gate.repo_checks import OWNER_ITEMS, REPO_CHECKS

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "weavelabs-pvt-ltd/weaves"
LABEL = {PASS: "PASS   ", FAIL: "FAIL   ", BLOCKED: "BLOCKED", UNVERIFIED: "UNVERIF."}


def github_protected(repository: str = REPOSITORY) -> bool | None:
    """Ask GitHub (read-only GET) whether ``main`` is protected; None if unknown."""
    command = ["gh", "api", f"repos/{repository}/branches/main", "--jq", ".protected"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    answer = done.stdout.strip()
    return {"true": True, "false": False}.get(answer) if done.returncode == 0 else None


def evaluate(ctx: Context) -> list[Result]:
    """Run every check; a crashing check is reported as a failure, never hidden."""
    results: list[Result] = []
    for check in (*REPO_CHECKS, *OWNER_ITEMS, *PRODUCTION_CHECKS):
        try:
            results.append(check(ctx))
        except Exception as error:  # a broken check must not hide the others
            name = getattr(check, "__name__", "check")
            results.append(Result(name, name, FAIL, f"check crashed: {error!r}"))
    return results


def exit_status(results: list[Result]) -> int:
    """0 all pass, 1 any fail, 2 only blocked or unverified items remain."""
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return 1
    return 0 if statuses == {PASS} else 2


def render(results: list[Result]) -> str:
    """The human report."""
    lines = ["Weaves private-beta gate (read-only report)", ""]
    for item in results:
        lines.append(f"{LABEL[item.status]} {item.id:<4} {item.title}")
        lines.append(f"{'':13}{item.detail}")
        if item.next_step:
            lines.append(f"{'':13}next: {item.next_step}")
    counts = {s: sum(r.status == s for r in results) for s in STATUSES}
    verdict = {
        0: "ALL ITEMS PASS",
        1: "NOT READY: failures",
        2: "NOT READY: open items",
    }
    lines += [
        "",
        "  ".join(f"{s}: {n}" for s, n in counts.items()),
        verdict[exit_status(results)] + ". Public signup stays off; this report "
        "approves nothing.",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Parse arguments, evaluate and print the report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--production-state", type=Path)
    parser.add_argument("--github", action="store_true", help="read branch protection")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    production = load_json(args.production_state) if args.production_state else None
    if args.production_state and production is None:
        print("unreadable --production-state file", file=sys.stderr)
        return 1
    ctx = Context(
        root=args.root,
        evidence_dir=args.evidence_dir or args.root / ".local/evidence",
        production=production,
        github_protected=github_protected() if args.github else None,
        now=datetime.now(UTC),
    )
    results = evaluate(ctx)
    if args.as_json:
        print(json.dumps([r.as_json() for r in results], indent=2))
    else:
        print(render(results))
    return exit_status(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
