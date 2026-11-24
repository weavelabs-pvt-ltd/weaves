"""Environment-backed limit settings for Phase 5 (limits, fairness, usage).

Defaults are sized for one ``e2-medium`` VM (2 shared vCPU, 4 GB) that also runs
PostgreSQL. Slice 5.6 measured the capacity-relevant ones on a local stack
capped to that shape (``docs/capacity-envelope.md``, simulated, not measured on
GCP); each comment below says which defaults are measurement-backed and which are
product policy that capacity does not decide. Nothing else should hard-code a
number. ``WEAVES_LIMITS_MODE`` defaults to ``observe`` so a consulting slice logs
the would-be refusal instead of changing behaviour; the per-organization running
cap, and so tenant isolation, only takes effect in ``enforce``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

LimitsMode = Literal["off", "observe", "enforce"]
_MODES = ("off", "observe", "enforce")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}
_BIG = 1_000_000_000


class LimitsConfigError(ValueError):
    """A limit setting is malformed or outside its bounds."""


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return None if value is None or not value.strip() else value.strip()


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = int(raw.replace(",", ""))
    except ValueError:
        raise LimitsConfigError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise LimitsConfigError(f"{name} must be between {low} and {high}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise LimitsConfigError(f"{name} must be true or false")


def _cost(env: Mapping[str, str], name: str, default: float | None) -> float | None:
    raw = _text(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise LimitsConfigError(f"{name} must be a number") from None
    if not math.isfinite(value) or not 0 < value <= 1_000_000:
        raise LimitsConfigError(f"{name} must be between 0 and 1000000")
    return value


def _mode(raw: str) -> LimitsMode:
    value = raw.strip().lower()
    if value not in _MODES:
        raise LimitsConfigError("WEAVES_LIMITS_MODE must be off, observe or enforce")
    return cast(LimitsMode, value)


@dataclass(frozen=True)
class LimitsConfig:
    """Every Phase 5 setting; see the module docstring for what was measured."""

    # Ship measuring first; flip to ``enforce`` per environment. Measured (5.6): with
    # four slots, ``observe`` lets one organization hold every slot (another
    # tenant's queue delay p95 20 s) while ``enforce`` keeps it at 1.2 s.
    mode: LimitsMode = "observe"
    # Global (per worker process). Measured (5.6): four slots with a running cap of
    # two keep a normal tenant's queue delay p95 at 1.2 s beside a saturated one
    # (18.6 s with two slots); worker memory stays near 55 MiB either way.
    worker_slots: int = 4
    # Round-robin claiming across organizations with the per-organization
    # running cap. ``False`` restores the global oldest-first claim.
    fair_claim: bool = True
    # Half of the four slots, so one organization never takes the whole worker.
    # With two slots use 1 (measured: p95 2.8 s, at half the hot tenant's rate).
    max_running_per_org: int = 2
    # Bounds claim enumeration and drain time: a full queue of 25 s calls drains in
    # about 10 minutes at the running cap. Claim delay was flat from 10 to 200
    # organizations with work queued, so the cap is policy, not a speed limit.
    max_queued_per_org: int = 50
    # Emergency brake across all organizations (ten full organization queues).
    global_max_queued: int = 500
    # Operator kill switch for new work; reads and cancellation keep working.
    admissions_paused: bool = False
    # Home polls ~40/min per open tab; leave ten times headroom. Token bucket.
    # Protection against one tenant, not an aggregate cap: measured (5.6) the
    # single API process serves about 53 requests/s (about 3,200/min) of Home
    # polling, so a handful of organizations at 600/min would saturate it.
    requests_per_minute_org: int = 600
    requests_per_minute_principal: int = 240
    request_burst: int = 100
    # Product policy (capacity does not decide it); counts non-deleted users.
    seats: int = 25
    # Each schedule can enqueue every few minutes.
    schedules: int = 20
    # Bound catalog list cost (and the 5.4 page walks); /agents answered in 40 ms
    # p95 at 200 organizations.
    agents: int = 50
    workflows: int = 50
    # Per-run cap is <=100, so about 20 full runs of tool calls per day.
    tool_calls_per_day: int = 2_000
    # About 400 runs at 5k tokens; BYOK so this is capacity control.
    tokens_per_day: int = 2_000_000
    # About 20 days at the daily cap.
    tokens_per_month: int = 40_000_000
    # Stops a runaway loop on a priced profile; ``None`` rates are skipped.
    est_cost_usd_per_day: float | None = 25.0
    # Open plus archived tasks, a proxy for storage until a byte rollup exists.
    # Measured (5.6) at 6,000 tasks: Home 41 ms p50, a page 45 ms; 10,000 untested
    # beyond the index plans.
    storage_tasks: int = 10_000
    # Conservative estimate; settlement corrects it.
    reservation_input_chars_per_token: int = 3
    # Oldest queued work across all tenants. A saturated tenant at its queue cap
    # legitimately waits 5 to 7 minutes (measured 313 to 434 s), so the 120 s
    # starting point would warn on healthy saturation; 900 s still catches a
    # stuck worker. The service envelope itself is guarded by the running cap.
    queue_age_warn_seconds: int = 900
    # Matches the existing task and audit pages.
    page_default: int = 50
    page_max: int = 100
    # Matches the existing account-purge batch size.
    recovery_batch: int = 200
    # A year of billing reconciliation plus margin.
    usage_retention_days: int = 400

    def __post_init__(self) -> None:
        """Reject inconsistent combinations (also for hand-built configs)."""
        _mode(self.mode)
        if self.page_default > self.page_max:
            raise LimitsConfigError("WEAVES_PAGE_DEFAULT cannot exceed WEAVES_PAGE_MAX")
        if self.max_running_per_org > self.max_queued_per_org:
            raise LimitsConfigError(
                "WEAVES_LIMIT_MAX_RUNNING_PER_ORG cannot exceed "
                "WEAVES_LIMIT_MAX_QUEUED_PER_ORG"
            )
        if self.tokens_per_day > self.tokens_per_month:
            raise LimitsConfigError(
                "WEAVES_LIMIT_TOKENS_PER_DAY cannot exceed "
                "WEAVES_LIMIT_TOKENS_PER_MONTH"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LimitsConfig:
        """Parse and bound-check every setting; raise ``LimitsConfigError``."""
        d = cls()
        return cls(
            mode=_mode(_text(env, "WEAVES_LIMITS_MODE") or d.mode),
            worker_slots=_int(env, "WEAVES_WORKER_SLOTS", d.worker_slots, 1, 32),
            fair_claim=_bool(env, "WEAVES_FAIR_CLAIM", d.fair_claim),
            max_running_per_org=_int(
                env, "WEAVES_LIMIT_MAX_RUNNING_PER_ORG", d.max_running_per_org, 1, 32
            ),
            max_queued_per_org=_int(
                env, "WEAVES_LIMIT_MAX_QUEUED_PER_ORG", d.max_queued_per_org, 1, 100_000
            ),
            global_max_queued=_int(
                env, "WEAVES_LIMIT_GLOBAL_MAX_QUEUED", d.global_max_queued, 1, 1_000_000
            ),
            admissions_paused=_bool(
                env, "WEAVES_ADMISSIONS_PAUSED", d.admissions_paused
            ),
            requests_per_minute_org=_int(
                env,
                "WEAVES_LIMIT_REQUESTS_PER_MINUTE",
                d.requests_per_minute_org,
                1,
                1_000_000,
            ),
            requests_per_minute_principal=_int(
                env,
                "WEAVES_LIMIT_REQUESTS_PER_MINUTE_PER_PRINCIPAL",
                d.requests_per_minute_principal,
                1,
                1_000_000,
            ),
            request_burst=_int(
                env, "WEAVES_LIMIT_REQUEST_BURST", d.request_burst, 1, 100_000
            ),
            seats=_int(env, "WEAVES_LIMIT_SEATS", d.seats, 1, 100_000),
            schedules=_int(env, "WEAVES_LIMIT_SCHEDULES", d.schedules, 0, 100_000),
            agents=_int(env, "WEAVES_LIMIT_AGENTS", d.agents, 0, 100_000),
            workflows=_int(env, "WEAVES_LIMIT_WORKFLOWS", d.workflows, 0, 100_000),
            tool_calls_per_day=_int(
                env, "WEAVES_LIMIT_TOOL_CALLS_PER_DAY", d.tool_calls_per_day, 1, _BIG
            ),
            tokens_per_day=_int(
                env, "WEAVES_LIMIT_TOKENS_PER_DAY", d.tokens_per_day, 1, _BIG
            ),
            tokens_per_month=_int(
                env, "WEAVES_LIMIT_TOKENS_PER_MONTH", d.tokens_per_month, 1, _BIG
            ),
            est_cost_usd_per_day=_cost(
                env, "WEAVES_LIMIT_EST_COST_USD_PER_DAY", d.est_cost_usd_per_day
            ),
            storage_tasks=_int(
                env, "WEAVES_LIMIT_STORAGE_TASKS", d.storage_tasks, 1, _BIG
            ),
            reservation_input_chars_per_token=_int(
                env,
                "WEAVES_USAGE_RESERVATION_INPUT_CHARS_PER_TOKEN",
                d.reservation_input_chars_per_token,
                1,
                16,
            ),
            queue_age_warn_seconds=_int(
                env,
                "WEAVES_QUEUE_AGE_WARN_SECONDS",
                d.queue_age_warn_seconds,
                1,
                86_400,
            ),
            page_default=_int(env, "WEAVES_PAGE_DEFAULT", d.page_default, 1, 1_000),
            page_max=_int(env, "WEAVES_PAGE_MAX", d.page_max, 1, 1_000),
            recovery_batch=_int(
                env, "WEAVES_RECOVERY_BATCH", d.recovery_batch, 1, 10_000
            ),
            usage_retention_days=_int(
                env, "WEAVES_USAGE_RETENTION_DAYS", d.usage_retention_days, 1, 3_650
            ),
        )
