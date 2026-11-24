"""Model pricing rules.

A cost limit (an agent's ``max_run_cost_usd`` or a profile's ``cost_budget_usd``) is
enforced by pricing the tokens a run uses, so it is only enforceable on a model profile
that has both input and output token prices. These rules are checked wherever a cost
limit or a price is written, and again at run time for records written before they
existed.
"""

from __future__ import annotations

from decimal import Decimal

from contracts.v1 import ModelProfile
from shared.errors import ConfigurationRejected

COST_LIMIT_NEEDS_PRICING = "model.cost_limit_needs_pricing"
PRICING_INCOMPLETE = "model.pricing_incomplete"
# Profile fields whose change must re-check that cost limits stay enforceable.
PRICING_FIELDS = frozenset(
    {
        "cost_budget_usd",
        "input_cost_per_million_tokens_usd",
        "output_cost_per_million_tokens_usd",
    }
)


def is_priced(profile: ModelProfile) -> bool:
    """Return whether the profile has both token prices."""
    return (
        profile.input_cost_per_million_tokens_usd is not None
        and profile.output_cost_per_million_tokens_usd is not None
    )


def ensure_pricing_is_complete(profile: ModelProfile) -> None:
    """Reject a profile that has one token price but not the other."""
    has_input = profile.input_cost_per_million_tokens_usd is not None
    has_output = profile.output_cost_per_million_tokens_usd is not None
    if has_input != has_output:
        raise ConfigurationRejected(
            PRICING_INCOMPLETE,
            f"Set both the input and output token prices for {profile.model}, "
            "or neither.",
        )


def ensure_cost_limits_are_enforceable(
    profile: ModelProfile, agent_cost_limit_usd: Decimal | None = None
) -> None:
    """Reject a cost limit that cannot be enforced because the model is unpriced."""
    if is_priced(profile):
        return
    if agent_cost_limit_usd is not None:
        raise ConfigurationRejected(
            COST_LIMIT_NEEDS_PRICING,
            f"This agent has a cost limit, but the model {profile.model} has no "
            "token prices. Set its input and output prices in AI & Models, or "
            "remove the agent's cost limit.",
        )
    if profile.cost_budget_usd is not None:
        raise ConfigurationRejected(
            COST_LIMIT_NEEDS_PRICING,
            f"The model {profile.model} has a cost limit but no token prices. "
            "Set its input and output prices in AI & Models, or remove its cost "
            "limit.",
        )
