"""Gate checks that need only the repository and local evidence files."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from beta_gate.model import (
    Context,
    Result,
    age_days,
    blocked,
    load_json,
    make,
)

RUNBOOKS = (
    "incident-response",
    "tenant-deletion-and-erasure",
    "tenant-recovery",
    "disaster-recovery",
    "release-and-rollback",
    "kill-switch-and-quarantine",
    "secrets-rotation",
    "connector-app-registration",
)
DRILL_MAX_DAYS = 30
ROLLBACK_MAX_DAYS = 90
GATE_MARKER = re.compile(r"<!--\s*gate:\s*([^>]*?)\s*-->")
COMPOSE_DEFAULT = r"{name}:\s*\$\{{[A-Z_]+:-([^}}]*)\}}"


def runbooks(ctx: Context) -> Result:
    """Every required runbook exists and is not a stub."""
    missing = [
        name for name in RUNBOOKS if len(_text(ctx, f"docs/runbooks/{name}.md")) < 1500
    ]
    return make(
        "R01",
        "Runbooks present",
        not missing,
        f"all {len(RUNBOOKS)} present"
        if not missing
        else "missing or stub: " + ", ".join(missing),
        "write docs/runbooks/<name>.md",
    )


def _text(ctx: Context, relative: str) -> str:
    try:
        return ctx.file(relative).read_text()
    except OSError:
        return ""


def capacity_doc(ctx: Context) -> Result:
    """The capacity doc exists and declares settings the compose file defaults to."""
    declared = capacity_declaration(ctx)
    if declared is None:
        return make(
            "R02",
            "Supported capacity declared",
            False,
            "docs/supported-capacity.md has no `<!-- gate: ... -->` declaration",
            "restore the declaration line in docs/supported-capacity.md",
        )
    compose = _text(ctx, "infra/gcp/singlevm/compose.yaml")
    actual = {
        "worker_slots": _compose_default(compose, "WEAVES_WORKER_SLOTS"),
        "limits_mode": _compose_default(compose, "WEAVES_LIMITS_MODE"),
        "max_running_per_org": _compose_default(
            compose, "WEAVES_LIMIT_MAX_RUNNING_PER_ORG"
        ),
    }
    wrong = [
        f"{key}: doc {declared.get(key)!r} vs compose {value!r}"
        for key, value in actual.items()
        if declared.get(key) != value
    ]
    return make(
        "R02",
        "Supported capacity matches the checked-in deployment defaults",
        not wrong,
        "; ".join(wrong) or "slots, limits mode and running cap agree",
        "update docs/supported-capacity.md or compose.yaml so they agree",
    )


def capacity_declaration(ctx: Context) -> dict[str, str] | None:
    """The machine-readable ``gate:`` line of the capacity doc."""
    match = GATE_MARKER.search(_text(ctx, "docs/supported-capacity.md"))
    if match is None:
        return None
    pairs = (item.split("=", 1) for item in match.group(1).split() if "=" in item)
    return {key: value for key, value in pairs}


def _compose_default(compose: str, name: str) -> str | None:
    match = re.search(COMPOSE_DEFAULT.format(name=name), compose)
    return match.group(1) if match else None


def required_controls(ctx: Context) -> Result:
    """The pinned harness requires the isolation, usage and kill-switch controls."""
    pair = load_json(ctx.file("tenancy_harness/pair.json")) or {}
    required = set(pair.get("required_controls", []))
    need = {"T0-I01", "T1-I01", "T4-I01", "T5-I01", "T5-I05", "T6-I03"}
    missing = sorted(need - required)
    return make(
        "R03",
        "Harness requires T0-T5 isolation controls and the T6 kill switch",
        not missing and not pair.get("planned_controls"),
        f"{len(required)} required controls"
        if not missing
        else "not required in pair.json: " + ", ".join(missing),
        "add the control ids to tenancy_harness/pair.json required_controls",
    )


def release_policy(ctx: Context) -> Result:
    """The rollback policy is valid and pins a rehearsal revision."""
    policy = load_json(ctx.file("infra/gcp/singlevm/release_policy.json"))
    revision = str((policy or {}).get("min_rollback_revision"))
    sha = re.fullmatch(r"[0-9a-f]{40}", revision)
    floor = (policy or {}).get("min_rollback_schema_version")
    ok = bool(sha) and isinstance(floor, int) and floor >= 1
    return make(
        "R04",
        "Rollback floor pinned (min_rollback_revision and schema version)",
        ok,
        f"schema floor {floor}, revision {revision[:12]}",
        "fix infra/gcp/singlevm/release_policy.json",
    )


def _evidence(
    ctx: Context, name: str, limit: int, label: str
) -> tuple[dict[str, Any] | None, str]:
    document = load_json(ctx.evidence_dir / name)
    if document is None:
        return None, f"no {name} in {ctx.evidence_dir}"
    days = age_days(document.get("started"), ctx.now)
    if document.get("result") != "passed":
        return None, f"{label} did not pass"
    if days is None or days > limit:
        shown = "unreadable date" if days is None else f"{days:.0f} days old"
        return None, f"{label} evidence is {shown} (limit {limit})"
    return document, f"passed {days:.0f} days ago"


def rollback_rehearsal(ctx: Context) -> Result:
    """A recent passing release rehearsal manifest (CI artifact or local run)."""
    from rehearsal.manifest import gate_problems, load_policy

    document, detail = _evidence(
        ctx, "release-compat.json", ROLLBACK_MAX_DAYS, "rollback rehearsal"
    )
    if document is not None:
        try:
            policy = load_policy(ctx.file("infra/gcp/singlevm/release_policy.json"))
            revision = document.get("candidate", {}).get("revision", "")
            problems = gate_problems(document, policy, revision)
        except (OSError, ValueError) as error:
            problems = [type(error).__name__]
        if problems:
            document, detail = None, "; ".join(problems)
    return make(
        "R05",
        f"Rollback rehearsal evidence (at most {ROLLBACK_MAX_DAYS} days old)",
        document is not None,
        detail,
        "python scripts/release_rehearsal.py run --output "
        f"{ctx.evidence_dir}/release-compat.json (or download the CI artifact)",
    )


def local_drill(ctx: Context) -> Result:
    """A recent passing local disaster-recovery drill."""
    document, detail = _evidence(
        ctx, "dr-drill.json", DRILL_MAX_DAYS, "disaster-recovery drill"
    )
    if document is not None:
        rto = document.get("rto_seconds")
        gap = document.get("backup_to_disaster_seconds")
        detail += f" (RTO {rto} s; backup-to-disaster {gap} s)"
    return make(
        "R06",
        f"Local disaster-recovery drill evidence (at most {DRILL_MAX_DAYS} days old)",
        document is not None,
        detail,
        f"python scripts/dr_drill.py --output {ctx.evidence_dir}/dr-drill.json",
    )


def tombstones_and_switches(ctx: Context) -> Result:
    """Restore reconciliation, tombstones and the kill switch are in the checkout."""
    needed = (
        "infra/gcp/singlevm/restore_reconcile.py",
        "infra/gcp/singlevm/platform_control.py",
        "modules/recovery/tombstones.py",
        "modules/platform_controls/controls.py",
    )
    missing = [path for path in needed if not ctx.file(path).exists()]
    compose = _text(ctx, "infra/gcp/singlevm/compose.yaml")
    if "WEAVES_TOMBSTONE_DIR" not in compose:
        missing.append("WEAVES_TOMBSTONE_DIR in compose.yaml")
    return make(
        "R07",
        "Tombstones, restore reconciliation and kill switch shipped",
        not missing,
        "present" if not missing else "missing: " + ", ".join(missing),
        "restore the missing slice (6.2 or 6.4)",
    )


def signup_defaults(ctx: Context) -> Result:
    """The checked-in deployment keeps public registration closed by default."""
    compose = _text(ctx, "infra/gcp/singlevm/compose.yaml")
    registration = re.search(r'WEAVES_PUBLIC_REGISTRATION:\s*"false"', compose)
    creation = _compose_default(compose, "WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION")
    ok = bool(registration) and creation == "false"
    return make(
        "R08",
        "Checked-in deployment: public registration off, org creation default false",
        ok,
        "compose.yaml keeps both closed" if ok else "compose.yaml opens signup",
        "restore the closed defaults in infra/gcp/singlevm/compose.yaml",
    )


def _absent(path: str, item: str, title: str, slice_name: str, ask: str) -> Check:
    def check(ctx: Context) -> Result:
        if ctx.file(path).exists():
            return make(item, title, True, f"{path} exists")
        return blocked(
            item,
            title,
            f"{slice_name} is not built or not approved: {path} is absent",
            ask,
        )

    return check


Check = Callable[[Context], Result]

OWNER_ITEMS: tuple[Check, ...] = (
    _absent(
        "infra/gcp/alerts",
        "O02",
        "Alert policies and recipients (6.5)",
        "6.5",
        "owner chooses notification channels and recipients",
    ),
    _absent(
        "infra/gcp/staging",
        "O03",
        "Staging environment for restore drills (6.6)",
        "6.6",
        "owner approves a disposable staging VM or project",
    ),
)

REPO_CHECKS: tuple[Check, ...] = (
    runbooks,
    capacity_doc,
    required_controls,
    release_policy,
    rollback_rehearsal,
    local_drill,
    tombstones_and_switches,
    signup_defaults,
)
