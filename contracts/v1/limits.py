"""Published limit contracts: the read-only usage view and the operator override."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from contracts.v1.base import (
    FrozenProductContract,
    MutableOrgScopedContract,
    OpaqueId,
)

LimitsModeName = Literal["off", "observe", "enforce"]
LimitResourceName = Literal[
    "queued_work",
    "tasks",
    "seats",
    "schedules",
    "agents",
    "workflows",
    "tool_calls_per_day",
]


class LimitUsageView(FrozenProductContract):
    """One limit with the organization's current consumption of it."""

    resource: LimitResourceName
    limit: int = Field(ge=0)
    used: int = Field(ge=0)


class OrganizationLimits(FrozenProductContract):
    """What an organization may use and what it uses now (own data only)."""

    mode: LimitsModeName
    admissions_paused: bool
    requests_per_minute: int = Field(ge=0)
    limits: tuple[LimitUsageView, ...]


class OrganizationLimitOverride(MutableOrgScopedContract):
    """Operator-set caps for one organization; ``None`` keeps the default.

    One row per organization, keyed by ``org_id``. Written only by the operator
    script, never over HTTP.
    """

    org_id: OpaqueId
    max_queued_per_org: int | None = Field(default=None, ge=1, le=100_000)
    requests_per_minute_org: int | None = Field(default=None, ge=1, le=1_000_000)
    requests_per_minute_principal: int | None = Field(default=None, ge=1, le=1_000_000)
    request_burst: int | None = Field(default=None, ge=1, le=100_000)
    seats: int | None = Field(default=None, ge=1, le=100_000)
    schedules: int | None = Field(default=None, ge=0, le=100_000)
    agents: int | None = Field(default=None, ge=0, le=100_000)
    workflows: int | None = Field(default=None, ge=0, le=100_000)
    tool_calls_per_day: int | None = Field(default=None, ge=1, le=1_000_000_000)
    storage_tasks: int | None = Field(default=None, ge=1, le=1_000_000_000)
    # Model-usage caps (slice 5.3): settled plus outstanding usage per UTC day/month.
    tokens_per_day: int | None = Field(default=None, ge=1, le=1_000_000_000)
    tokens_per_month: int | None = Field(default=None, ge=1, le=1_000_000_000)
    updated_by: str = Field(min_length=1, max_length=320)
    reason: str = Field(min_length=1, max_length=500)
