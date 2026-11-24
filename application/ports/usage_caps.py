"""Admission arithmetic shared by every usage ledger adapter."""

from __future__ import annotations

from dataclasses import dataclass

from application.ports.usage import UsageAdmission, UsageReservationRequest


@dataclass(frozen=True)
class Committed:
    """Usage already counted against the caps (settled plus outstanding)."""

    day_tokens: int
    month_tokens: int
    day_cost_nano_usd: int


def breached_cap(
    request: UsageReservationRequest,
    admission: UsageAdmission,
    committed: Committed,
) -> str | None:
    """Name the first cap that committed usage plus this reservation exceeds."""
    new = request.reserved_input_tokens + request.reserved_output_tokens
    if committed.day_tokens + new > admission.tokens_per_day:
        return "tokens_per_day"
    if committed.month_tokens + new > admission.tokens_per_month:
        return "tokens_per_month"
    cap = admission.cost_nano_usd_per_day
    cost = request.reserved_cost_nano_usd
    if (
        cap is not None
        and cost is not None
        and committed.day_cost_nano_usd + cost > cap
    ):
        return "est_cost_per_day"
    return None
