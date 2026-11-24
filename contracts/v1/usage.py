"""Per-organization usage ledger records and the usage report contract.

Money is integer nano-USD (1 USD = 1_000_000_000) and never a float. Tokens are
exactly what the provider reported. This records what customers spend on their
own provider keys (BYOK); it does not charge anyone.
"""

from __future__ import annotations

from datetime import date

from pydantic import AwareDatetime, Field

from contracts.v1.base import FrozenProductContract, OpaqueId, StrEnum

NANO_USD_PER_USD = 1_000_000_000


class UsageState(StrEnum):
    """Lifecycle of one model call's usage record."""

    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"
    UNKNOWN = "unknown"


class UsageGroupBy(StrEnum):
    """How the usage report groups its rows."""

    AGENT = "agent"
    MODEL = "model"
    DAY = "day"


class UsageEvent(FrozenProductContract):
    """One ledger row, keyed by organization and ``reservation_key``."""

    org_id: OpaqueId
    workspace_id: OpaqueId
    reservation_key: OpaqueId
    agent_run_id: OpaqueId
    run_id: OpaqueId
    agent_id: OpaqueId
    provider_id: OpaqueId | None = None
    model_id: OpaqueId | None = None
    state: UsageState
    reserved_input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    reserved_cost_nano_usd: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_nano_usd: int | None = Field(default=None, ge=0)
    outcome: str | None = Field(default=None, max_length=64)
    reserved_at: AwareDatetime
    settled_at: AwareDatetime | None = None


class UsageTotals(FrozenProductContract):
    """Tokens and estimated cost over a set of settled calls."""

    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_nano_usd: int = Field(ge=0)
    # Calls whose model has no price: their tokens count, their cost is absent.
    unpriced_calls: int = Field(ge=0)


class UsageGroup(UsageTotals):
    """Totals for one agent, model or day."""

    key: str = Field(min_length=1, max_length=256)


class UsageSummary(FrozenProductContract):
    """The ``GET /usage`` response for one organization."""

    from_date: date
    to_date: date
    group_by: UsageGroupBy
    totals: UsageTotals
    groups: tuple[UsageGroup, ...]
    # Calls that may have reached the provider but have no known token count.
    unknown_calls: int = Field(ge=0)
    # Calls reserved and still running.
    in_flight_calls: int = Field(ge=0)


__all__ = [
    "NANO_USD_PER_USD",
    "UsageEvent",
    "UsageGroup",
    "UsageGroupBy",
    "UsageState",
    "UsageSummary",
    "UsageTotals",
]
