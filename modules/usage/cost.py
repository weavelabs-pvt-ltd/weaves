"""Token and cost arithmetic for the usage ledger (integers, never floats).

Model profiles price tokens in USD per million. The ledger stores integer
nano-USD (1 USD = 1_000_000_000), so ``1 token * 1 USD/M`` is 1000 nano-USD.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from math import ceil

from contracts.v1.usage import NANO_USD_PER_USD

_MILLION = Decimal(1_000_000)


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_rate_per_million: Decimal | None,
    output_rate_per_million: Decimal | None,
) -> Decimal | None:
    """Exact USD cost, or ``None`` when the model profile has no complete price."""
    if input_rate_per_million is None or output_rate_per_million is None:
        return None
    return (
        Decimal(input_tokens) * input_rate_per_million
        + Decimal(output_tokens) * output_rate_per_million
    ) / _MILLION


def usd_to_nano(cost: Decimal) -> int:
    """Round a USD amount to whole nano-USD."""
    return int((cost * NANO_USD_PER_USD).to_integral_value(ROUND_HALF_UP))


def estimate_cost_nano_usd(
    input_tokens: int,
    output_tokens: int,
    input_rate_per_million: Decimal | None,
    output_rate_per_million: Decimal | None,
) -> int | None:
    """Cost in nano-USD, or ``None`` when unpriced."""
    cost = estimate_cost_usd(
        input_tokens, output_tokens, input_rate_per_million, output_rate_per_million
    )
    return None if cost is None else usd_to_nano(cost)


def estimate_input_tokens(characters: int, characters_per_token: int) -> int:
    """Conservative prompt estimate used only to size a reservation."""
    return max(1, ceil(characters / characters_per_token))
