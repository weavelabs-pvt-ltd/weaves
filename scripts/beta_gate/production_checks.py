"""Gate checks about the running system, from operator-collected evidence only.

The gate never connects to production. An operator runs the read-only collector
(``scripts/gate_collect_production.py``, streamed to the VM) and gives its JSON
to ``--production-state``. Without it these items are ``unverified``.
"""

from __future__ import annotations

from beta_gate.model import (
    BLOCKED,
    FAIL,
    PASS,
    Context,
    Result,
    age_days,
    blocked,
    make,
    unverified,
)
from beta_gate.repo_checks import capacity_declaration

COLLECT = (
    "operator: collect state with scripts/gate_collect_production.py "
    "(docs/private-beta-gate.md), then pass --production-state"
)
MAX_STATE_DAYS = 2.0
BACKUP_MAX_HOURS = 26.0


def _need_state(ctx: Context, item: str, title: str) -> Result | None:
    """An UNVERIFIED result when evidence is absent or stale, else None."""
    if ctx.production is None:
        return unverified(item, title, "no --production-state supplied", COLLECT)
    days = age_days(ctx.production.get("collected_at"), ctx.now)
    if days is None or days > MAX_STATE_DAYS:
        return unverified(
            item, title, "the production state is older than 2 days", COLLECT
        )
    return None


def _env(ctx: Context, name: str) -> str | None:
    values = (ctx.production or {}).get("env", {})
    value = values.get(name) if isinstance(values, dict) else None
    return None if value is None else str(value)


def limits_mode(ctx: Context) -> Result:
    """Per-organization caps are enforced, not only logged."""
    item, title = "P01", "Limits mode is enforce"
    if (early := _need_state(ctx, item, title)) is not None:
        return early
    mode = _env(ctx, "WEAVES_LIMITS_MODE") or "observe"
    if mode == "enforce":
        return Result(item, title, PASS, "WEAVES_LIMITS_MODE=enforce")
    return blocked(
        item,
        title,
        f"WEAVES_LIMITS_MODE={mode}; enforce is pending the owner's decision",
        "owner reads the observe log, then sets enforce in runtime.env "
        "(README 'Tenant limits'; its own deploy)",
    )


def signup_closed(ctx: Context) -> Result:
    """Outsiders cannot create accounts: allowlist set, registration off."""
    item, title = "P02", "Public signup stays off (allowlist set, registration off)"
    if (early := _need_state(ctx, item, title)) is not None:
        return early
    allowlist = bool((ctx.production or {}).get("allowlist_set"))
    registration = (_env(ctx, "WEAVES_PUBLIC_REGISTRATION") or "false").lower()
    creation = (
        _env(ctx, "WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION") or "false"
    ).lower()
    problems = []
    if not allowlist:
        problems.append("no account allowlist is set (anyone with Google can sign in)")
    if registration == "true":
        problems.append("WEAVES_PUBLIC_REGISTRATION is true")
    return make(
        item,
        title,
        not problems,
        "; ".join(problems)
        or f"allowlist set, registration {registration}, org creation {creation}",
        "set WEAVES_ACCOUNT_ALLOWED_EMAILS and keep registration false",
    )


def signup_switch(ctx: Context) -> Result:
    """The runtime signup switch is closed (design 6.10: signup_enabled=false)."""
    item, title = "P03", "Runtime signup switch is closed (signup_enabled=false)"
    if (early := _need_state(ctx, item, title)) is not None:
        return early
    controls = (ctx.production or {}).get("platform_controls") or {}
    effective = (controls.get("effective") or {}).get("signup_enabled")
    if effective is False:
        return Result(item, title, PASS, "effective signup_enabled is false")
    return Result(
        item,
        title,
        FAIL,
        f"effective signup_enabled is {effective!r}",
        "operator: platform_control.py set --signup-enabled false (README); "
        "the gate does not change it",
    )


def backups(ctx: Context) -> Result:
    """A fresh on-host dump exists (the only backup there is today)."""
    item, title = "P04", "Newest on-host backup is under 26 hours old"
    if (early := _need_state(ctx, item, title)) is not None:
        return early
    age = (ctx.production or {}).get("newest_backup_age_hours")
    if not isinstance(age, int | float):
        return Result(item, title, FAIL, "no backup file was found", "run backup.sh")
    return make(
        item,
        title,
        age <= BACKUP_MAX_HOURS,
        f"newest dump is {age:.1f} hours old",
        "check the 19:45 UTC cron on the VM and run backup.sh",
    )


def offhost_backup(ctx: Context) -> Result:
    """An off-host copy exists (needs the owner-approved slice 6.1)."""
    item, title = "P05", "Fresh off-host encrypted backup exists"
    age = (ctx.production or {}).get("offhost_backup_age_hours")
    if isinstance(age, int | float):
        return make(
            item,
            title,
            age <= BACKUP_MAX_HOURS,
            f"newest off-host copy {age:.1f} h old",
            "check the uploader and bucket",
        )
    return blocked(
        item,
        title,
        "backups exist only on the VM disk and in 7-day snapshots",
        "owner approves slice 6.1 (bucket, write-only service account, restart)",
    )


def capacity_matches_deployment(ctx: Context) -> Result:
    """The capacity doc matches what runs in production, not only the repo."""
    item, title = "P06", "Supported capacity matches the deployed limits"
    if (early := _need_state(ctx, item, title)) is not None:
        return early
    declared = capacity_declaration(ctx) or {}
    names = {
        "worker_slots": "WEAVES_WORKER_SLOTS",
        "max_running_per_org": "WEAVES_LIMIT_MAX_RUNNING_PER_ORG",
    }
    wrong = [
        f"{key}: doc {declared.get(key)!r} vs production {_env(ctx, name)!r}"
        for key, name in names.items()
        if _env(ctx, name) not in (None, declared.get(key))
    ]
    return make(
        item,
        title,
        not wrong,
        "; ".join(wrong) or "slots and running cap agree",
        "align runtime.env and docs/supported-capacity.md",
    )


def branch_protection(ctx: Context) -> Result:
    """Branch protection on main (a GitHub repository setting)."""
    item, title = "G01", "Branch protection on main with required checks"
    if ctx.github_protected is True:
        return Result(item, title, PASS, "main is protected")
    if ctx.github_protected is False:
        return Result(
            item,
            title,
            BLOCKED,
            "main is not protected; the owner has not approved enabling it",
            "owner enables branch protection and required checks",
        )
    return unverified(item, title, "run with --github to ask GitHub", "--github")


PRODUCTION_CHECKS = (
    limits_mode,
    signup_closed,
    signup_switch,
    backups,
    offhost_backup,
    capacity_matches_deployment,
    branch_protection,
)
