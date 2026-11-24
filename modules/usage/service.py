"""Reserve, settle and release usage around one provider call.

The model call site records intent before the request (``begin``) and the
provider's numbers after it (``settle``/``release``/``mark_unknown``). Every
step is keyed by the agent run id and is idempotent. Recording never fails a
run: a ledger error is logged and the reconciler repairs the row from the run
record. Only a reservation that would exceed an enforced cap refuses the call.
BYOK: customers pay their providers, so this records usage, it does not charge.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from application.ports.limits import LimitPolicy
from application.ports.usage import (
    UsageAdmission,
    UsageLedger,
    UsageReservationRequest,
    UsageSettlement,
)
from contracts.v1 import ModelRequest
from modules.usage.cost import (
    estimate_cost_nano_usd,
    estimate_input_tokens,
    usd_to_nano,
)
from shared.clock import utc_now
from shared.metrics import USAGE_TOKENS

logger = logging.getLogger("product.usage")

# Provider answers that mean no provider request was processed.
NOT_SENT_CODES = frozenset(
    {
        "model.not_allowed",
        "model.disabled",
        "model.adapter_missing",
        "model.credential_unavailable",
        "model.provider_unsupported",
        "model.request_invalid",
        "model.redirect_blocked",
    }
)
_REJECTED_BEFORE_PROCESSING = range(400, 500)
_REQUEST_TIMEOUT_STATUS = 408


class UsageLimitExceeded(RuntimeError):
    """An enforced usage cap would be exceeded by this call's reservation."""

    code = "limit_exceeded"

    def __init__(self, breach: str) -> None:
        """Name the cap that would have been exceeded."""
        super().__init__(f"The organization's usage limit ({breach}) was reached")
        self.breach = breach


@dataclass(frozen=True)
class UsageSettings:
    """Global settings from the limits configuration."""

    chars_per_token: int = 3
    retention_days: int = 400


@dataclass(frozen=True)
class UsageCall:
    """Who is calling which model, and at what price."""

    org_id: str
    workspace_id: str
    agent_run_id: str
    run_id: str
    agent_id: str
    provider_id: str | None
    model_id: str | None
    input_rate_per_million: Decimal | None
    output_rate_per_million: Decimal | None

    def settlement(
        self,
        input_tokens: int,
        output_tokens: int,
        outcome: str,
        now: datetime,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> UsageSettlement:
        """Build the settlement for provider-reported tokens."""
        return UsageSettlement(
            org_id=self.org_id,
            workspace_id=self.workspace_id,
            reservation_key=self.agent_run_id,
            agent_run_id=self.agent_run_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            provider_id=provider_id or self.provider_id,
            model_id=model_id or self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_nano_usd=estimate_cost_nano_usd(
                input_tokens,
                output_tokens,
                self.input_rate_per_million,
                self.output_rate_per_million,
            ),
            outcome=outcome,
            now=now,
        )


def classify_failure(code: str, http_status: int | None) -> str:
    """``released`` when the provider cannot have processed it, else ``unknown``."""
    if code in NOT_SENT_CODES:
        return "released"
    if (
        code == "model.upstream_error"
        and http_status in _REJECTED_BEFORE_PROCESSING
        and http_status != _REQUEST_TIMEOUT_STATUS
    ):
        return "released"
    return "unknown"


class UsageService:
    """Record one call's usage; never raises except for an enforced cap."""

    def __init__(
        self,
        ledger: UsageLedger,
        policy: LimitPolicy,
        settings: UsageSettings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the durable ledger, the per-organization limits and settings."""
        self.ledger = ledger
        self.policy = policy
        self.settings = settings
        self._clock = clock

    def begin(self, call: UsageCall, request: ModelRequest) -> None:
        """Reserve estimated usage before the provider request."""
        now = self._clock()
        limits = self.policy.for_org(call.org_id)
        characters = sum(len(message.content) for message in request.messages)
        reserved_in = estimate_input_tokens(characters, self.settings.chars_per_token)
        reserved_out = request.max_output_tokens
        reserved_cost = estimate_cost_nano_usd(
            reserved_in,
            reserved_out,
            call.input_rate_per_million,
            call.output_rate_per_million,
        )
        admission = None
        if limits.active:
            cap = limits.est_cost_usd_per_day
            admission = UsageAdmission(
                day_start=now.astimezone(UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
                month_start=now.astimezone(UTC).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                ),
                tokens_per_day=limits.tokens_per_day,
                tokens_per_month=limits.tokens_per_month,
                cost_nano_usd_per_day=(
                    None if cap is None else usd_to_nano(Decimal(str(cap)))
                ),
                enforce=limits.enforcing,
            )
        try:
            result = self.ledger.reserve(
                UsageReservationRequest(
                    org_id=call.org_id,
                    workspace_id=call.workspace_id,
                    reservation_key=call.agent_run_id,
                    agent_run_id=call.agent_run_id,
                    run_id=call.run_id,
                    agent_id=call.agent_id,
                    provider_id=call.provider_id,
                    model_id=call.model_id,
                    reserved_input_tokens=reserved_in,
                    reserved_output_tokens=reserved_out,
                    reserved_cost_nano_usd=reserved_cost,
                    now=now,
                ),
                admission,
            )
        except Exception:
            logger.exception("usage reservation failed run=%s", call.agent_run_id)
            return
        if result.breach is None:
            return
        if result.event is None:
            raise UsageLimitExceeded(result.breach)
        logger.warning(
            "usage limit would refuse org_id=%s run=%s limit=%s mode=%s",
            call.org_id,
            call.agent_run_id,
            result.breach,
            limits.mode,
        )

    def settle(
        self,
        call: UsageCall,
        input_tokens: int,
        output_tokens: int,
        outcome: str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        """Record provider-reported tokens (also for rejected outputs)."""
        try:
            self.ledger.settle(
                call.settlement(
                    input_tokens,
                    output_tokens,
                    outcome,
                    self._clock(),
                    provider_id=provider_id,
                    model_id=model_id,
                )
            )
        except Exception:
            logger.exception("usage settle failed run=%s", call.agent_run_id)

    def fail(
        self,
        call: UsageCall,
        code: str,
        *,
        http_status: int | None = None,
        usage: tuple[int, int] | None = None,
    ) -> None:
        """Resolve the reservation after a provider error."""
        if usage is not None:
            # The gateway counts tokens only for usable answers; this one was paid
            # for but rejected, so count it here (once) and nowhere else.
            USAGE_TOKENS.inc(usage[0], direction="input")
            USAGE_TOKENS.inc(usage[1], direction="output")
            self.settle(call, usage[0], usage[1], code)
            return
        try:
            action = (
                self.ledger.release
                if classify_failure(code, http_status) == "released"
                else self.ledger.mark_unknown
            )
            action(call.org_id, call.agent_run_id, outcome=code, now=self._clock())
        except Exception:
            logger.exception("usage resolve failed run=%s", call.agent_run_id)

    def release(self, call: UsageCall, outcome: str) -> None:
        """No provider request was made."""
        try:
            self.ledger.release(
                call.org_id, call.agent_run_id, outcome=outcome, now=self._clock()
            )
        except Exception:
            logger.exception("usage release failed run=%s", call.agent_run_id)
