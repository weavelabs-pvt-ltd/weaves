"""Per-organization limit lookup shared by admission, claiming and usage code.

Slices 5.1 to 5.5 read caps only through ``LimitPolicy`` so a later plan or
per-tenant override can replace the static environment values without touching
callers. Slice 5.0 provides the static implementation and nothing enforces it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from shared.limits_config import LimitsConfig, LimitsMode


@dataclass(frozen=True)
class OrgLimits:
    """The effective limits for one organization (and the global switches)."""

    mode: LimitsMode
    admissions_paused: bool
    global_max_queued: int
    max_running_per_org: int
    max_queued_per_org: int
    requests_per_minute_org: int
    requests_per_minute_principal: int
    request_burst: int
    seats: int
    schedules: int
    agents: int
    workflows: int
    tool_calls_per_day: int
    tokens_per_day: int
    tokens_per_month: int
    est_cost_usd_per_day: float | None
    storage_tasks: int

    @property
    def enforcing(self) -> bool:
        """True only when a breach must refuse work; ``observe`` just logs."""
        return self.mode == "enforce"

    @property
    def active(self) -> bool:
        """False when limits are switched off entirely."""
        return self.mode != "off"


class LimitPolicy(Protocol):
    """Resolve the limits that apply to an organization."""

    def for_org(self, org_id: str) -> OrgLimits:
        """Return the limits for ``org_id`` (an unknown id gets the defaults)."""
        ...


class StaticLimitPolicy:
    """Every organization gets the same environment-backed limits."""

    def __init__(self, config: LimitsConfig) -> None:
        """Resolve every organization to the limits derived from ``config``."""
        self.config = config
        self._limits = OrgLimits(
            mode=config.mode,
            admissions_paused=config.admissions_paused,
            global_max_queued=config.global_max_queued,
            max_running_per_org=config.max_running_per_org,
            max_queued_per_org=config.max_queued_per_org,
            requests_per_minute_org=config.requests_per_minute_org,
            requests_per_minute_principal=config.requests_per_minute_principal,
            request_burst=config.request_burst,
            seats=config.seats,
            schedules=config.schedules,
            agents=config.agents,
            workflows=config.workflows,
            tool_calls_per_day=config.tool_calls_per_day,
            tokens_per_day=config.tokens_per_day,
            tokens_per_month=config.tokens_per_month,
            est_cost_usd_per_day=config.est_cost_usd_per_day,
            storage_tasks=config.storage_tasks,
        )

    def for_org(self, org_id: str) -> OrgLimits:
        """Return the shared static limits."""
        return self._limits


def limit_policy_from_env(env: Mapping[str, str]) -> StaticLimitPolicy:
    """Build the process-wide policy; raises ``LimitsConfigError`` when invalid."""
    return StaticLimitPolicy(LimitsConfig.from_env(env))
