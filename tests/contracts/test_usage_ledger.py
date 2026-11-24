"""Usage ledger state machine, arithmetic and model-call metering (memory)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from application.ports.limits import StaticLimitPolicy
from application.ports.usage import (
    UsageAdmission,
    UsageReservationRequest,
    UsageSettlement,
)
from clients.model_providers.gateway import ModelGateway, ModelGatewayError
from contracts.v1 import (
    ModelProviderType,
    ModelResponse,
    ModelUsage,
    UsageGroupBy,
    UsageState,
)
from infrastructure.persistence.usage_ledger_memory import InMemoryUsageLedger
from modules.usage.cost import (
    estimate_cost_nano_usd,
    estimate_input_tokens,
    usd_to_nano,
)
from modules.usage.service import UsageSettings, classify_failure
from shared.limits_config import LimitsConfig
from tests.support.runtime import LocalPlatformRuntime

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def reservation(key="run-1", org="org-a", tokens=(100, 50), cost=None, **extra):
    return UsageReservationRequest(
        org_id=org,
        workspace_id="ws",
        reservation_key=key,
        agent_run_id=key,
        run_id="wf-" + key,
        agent_id=extra.get("agent_id", "agent-1"),
        provider_id="prov",
        model_id="model-x",
        reserved_input_tokens=tokens[0],
        reserved_output_tokens=tokens[1],
        reserved_cost_nano_usd=cost,
        now=extra.get("now", NOW),
    )


def settlement(key="run-1", org="org-a", tokens=(10, 5), cost=1_000, now=NOW, **kw):
    return UsageSettlement(
        org_id=org,
        workspace_id="ws",
        reservation_key=key,
        agent_run_id=key,
        run_id="wf-" + key,
        agent_id=kw.get("agent_id", "agent-1"),
        provider_id="prov",
        model_id="model-x",
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        estimated_cost_nano_usd=cost,
        outcome="ok",
        now=now,
    )


def test_cost_arithmetic_is_exact_integer_nano_usd():
    # 1 token at 1 USD per million is exactly 1000 nano-USD.
    assert estimate_cost_nano_usd(1, 0, Decimal(1), Decimal(1)) == 1_000
    assert estimate_cost_nano_usd(1000, 500, Decimal("3"), Decimal("15")) == 10_500_000
    assert estimate_cost_nano_usd(5, 5, None, Decimal(1)) is None
    assert usd_to_nano(Decimal("0.0000000004")) == 0
    assert usd_to_nano(Decimal("0.0000000005")) == 1
    assert estimate_input_tokens(10, 3) == 4 and estimate_input_tokens(0, 3) == 1


def test_reserve_is_idempotent_and_settle_counts_once():
    ledger = InMemoryUsageLedger()
    first = ledger.reserve(reservation(), None)
    again = ledger.reserve(reservation(), None)
    assert first.created and not again.created and again.event == first.event
    assert ledger.settle(settlement()) is True
    assert ledger.settle(settlement()) is False  # replay and retry: a no-op
    assert ledger.settle(settlement(tokens=(999, 999))) is False
    totals, groups, unknown, in_flight = ledger.summary(
        "org-a", date(2026, 11, 24), date(2026, 11, 24), UsageGroupBy.AGENT
    )
    assert (totals.calls, totals.input_tokens, totals.output_tokens) == (1, 10, 5)
    assert totals.estimated_cost_nano_usd == 1_000 and totals.total_tokens == 15
    assert [g.key for g in groups] == ["agent-1"] and (unknown, in_flight) == (0, 0)
    assert ledger.get("org-a", "run-1").state is UsageState.SETTLED


def test_release_unknown_and_late_settlement_never_double_count():
    ledger = InMemoryUsageLedger()
    ledger.reserve(reservation("released"), None)
    ledger.reserve(reservation("lost"), None)
    assert ledger.release("org-a", "released", outcome="model.disabled", now=NOW)
    assert not ledger.release("org-a", "released", outcome="x", now=NOW)
    assert ledger.mark_unknown("org-a", "lost", outcome="lease_expired", now=NOW)
    assert not ledger.mark_unknown("org-a", "lost", outcome="again", now=NOW)
    assert ledger.get("org-a", "lost").state is UsageState.UNKNOWN
    # A late real settlement upgrades unknown exactly once.
    assert ledger.settle(settlement("lost", tokens=(7, 3)))
    assert not ledger.settle(settlement("lost", tokens=(7, 3)))
    # Settling a settled row cannot be released or marked unknown afterwards.
    assert not ledger.release("org-a", "lost", outcome="x", now=NOW)
    totals, *_ = ledger.summary(
        "org-a", date(2026, 11, 24), date(2026, 11, 24), UsageGroupBy.DAY
    )
    assert totals.calls == 1 and totals.total_tokens == 10


def test_settle_without_a_reservation_upserts_the_row():
    ledger = InMemoryUsageLedger()
    assert ledger.settle(settlement("orphan"))
    event = ledger.get("org-a", "orphan")
    assert event.state is UsageState.SETTLED and event.input_tokens == 10


def test_keys_are_independent_per_organization():
    ledger = InMemoryUsageLedger()
    ledger.reserve(reservation("same", org="org-a"), None)
    other = ledger.reserve(reservation("same", org="org-b"), None)
    assert other.created
    ledger.settle(settlement("same", org="org-a", tokens=(1, 1)))
    assert ledger.get("org-b", "same").state is UsageState.RESERVED
    one, *_ = ledger.summary(
        "org-b", date(2026, 11, 1), date(2026, 11, 30), UsageGroupBy.DAY
    )
    assert one.calls == 0


def test_groups_by_agent_model_and_day_and_unpriced_calls():
    ledger = InMemoryUsageLedger()
    day2 = NOW + timedelta(days=1)
    ledger.settle(settlement("a", agent_id="agent-1"))
    ledger.settle(settlement("b", agent_id="agent-2", cost=None))
    ledger.settle(settlement("c", agent_id="agent-1", now=day2))
    window = (date(2026, 11, 24), date(2026, 11, 25))
    by_agent = ledger.summary("org-a", *window, UsageGroupBy.AGENT)[1]
    assert {g.key: g.calls for g in by_agent} == {"agent-1": 2, "agent-2": 1}
    by_day = ledger.summary("org-a", *window, UsageGroupBy.DAY)[1]
    assert [g.key for g in by_day] == ["2026-11-24", "2026-11-25"]
    by_model = ledger.summary("org-a", *window, UsageGroupBy.MODEL)
    assert [g.key for g in by_model[1]] == ["model-x"]
    assert by_model[0].unpriced_calls == 1


def test_admission_counts_settled_plus_outstanding_and_enforces():
    ledger = InMemoryUsageLedger()
    admission = UsageAdmission(
        day_start=NOW.replace(hour=0),
        month_start=NOW.replace(day=1, hour=0),
        tokens_per_day=300,
        tokens_per_month=10_000,
        cost_nano_usd_per_day=None,
        enforce=True,
    )
    assert ledger.reserve(reservation("a", tokens=(100, 50)), admission).created
    assert ledger.reserve(reservation("b", tokens=(100, 50)), admission).created
    refused = ledger.reserve(reservation("c", tokens=(1, 1)), admission)
    assert refused.event is None and refused.breach == "tokens_per_day"
    assert ledger.get("org-a", "c") is None
    # Settling real (smaller) usage frees the reserved headroom.
    ledger.settle(settlement("a", tokens=(10, 5)))
    ledger.settle(settlement("b", tokens=(10, 5)))
    assert ledger.reserve(reservation("c", tokens=(100, 50)), admission).created
    # Another organization is unaffected, and a replay is never refused.
    assert ledger.reserve(reservation("a", org="org-b"), admission).created
    assert ledger.reserve(reservation("c", tokens=(9999, 9999)), admission).event
    observed = admission.__class__(**{**admission.__dict__, "enforce": False})
    result = ledger.reserve(reservation("d", tokens=(500, 500)), observed)
    assert result.created and result.breach == "tokens_per_day"


def test_cost_cap_uses_reserved_cost_only_when_priced():
    ledger = InMemoryUsageLedger()
    admission = UsageAdmission(
        day_start=NOW.replace(hour=0),
        month_start=NOW.replace(day=1, hour=0),
        tokens_per_day=10**9,
        tokens_per_month=10**9,
        cost_nano_usd_per_day=1_000,
        enforce=True,
    )
    assert ledger.reserve(reservation("a", cost=600), admission).created
    assert ledger.reserve(reservation("b", cost=None), admission).created
    assert ledger.reserve(reservation("c", cost=600), admission).breach == (
        "est_cost_per_day"
    )


def test_purge_organization_and_retention():
    ledger = InMemoryUsageLedger()
    ledger.settle(settlement("old", now=NOW - timedelta(days=500)))
    ledger.settle(settlement("new"))
    ledger.reserve(reservation("open", now=NOW - timedelta(days=500)), None)
    assert ledger.purge_before(date(2026, 1, 1), 100) == 2  # row + its rollup
    assert ledger.get("org-a", "old") is None
    assert ledger.get("org-a", "open") is not None  # reserved rows are kept
    assert ledger.get("org-a", "new") is not None


def test_failure_classification():
    assert classify_failure("model.disabled", None) == "released"
    assert classify_failure("model.upstream_error", 401) == "released"
    assert classify_failure("model.upstream_error", 429) == "released"
    assert classify_failure("model.upstream_error", 408) == "unknown"
    assert classify_failure("model.upstream_error", 503) == "unknown"
    assert classify_failure("model.timeout", None) == "unknown"
    assert classify_failure("model.transport_error", None) == "unknown"


class ScriptedModel:
    """One scripted provider outcome per call."""

    def __init__(self, outcome, tokens=(11, 7), content="a reply"):
        self.outcome, self.tokens, self.content = outcome, tokens, content

    def complete(self, request, binding):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return ModelResponse(
            provider_id=binding.provider.id,
            model_id=binding.profile.model,
            content=self.content,
            usage=ModelUsage(
                input_tokens=self.tokens[0],
                output_tokens=self.tokens[1],
                total_tokens=sum(self.tokens),
            ),
        )


def runtime_with(model):
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    runtime.model_adapters[ModelProviderType.LOCAL] = model
    runtime.agent_execution_service.model_gateway = ModelGateway(runtime.model_adapters)
    return runtime


def only_row(runtime):
    rows = list(runtime.usage_ledger._rows.values())
    assert len(rows) == 1
    return rows[0]


def test_a_successful_run_records_exactly_the_provider_tokens():
    runtime = runtime_with(ScriptedModel(None, tokens=(11, 7)))
    result = runtime.run_agent("hello")
    row = only_row(runtime)
    assert row.state is UsageState.SETTLED and row.reservation_key == (
        result.agent_run.agent_run_id
    )
    assert (row.input_tokens, row.output_tokens) == (11, 7)
    assert (result.agent_run.input_tokens, result.agent_run.output_tokens) == (11, 7)
    assert row.reserved_output_tokens > 0 and row.reserved_input_tokens > 0


def test_provider_timeout_leaves_unknown_and_4xx_is_released():
    timeout = ModelGatewayError("model.timeout", "slow", retryable=True)
    runtime = runtime_with(ScriptedModel(timeout))
    with pytest.raises(ModelGatewayError):
        runtime.run_agent("hello")
    assert only_row(runtime).state is UsageState.UNKNOWN
    rejected = ModelGatewayError("model.upstream_error", "no", http_status=401)
    runtime = runtime_with(ScriptedModel(rejected))
    with pytest.raises(ModelGatewayError):
        runtime.run_agent("hello")
    assert only_row(runtime).state is UsageState.RELEASED


def test_unusable_answers_still_record_tokens_the_provider_reported():
    empty = ModelGatewayError("model.empty_response", "none", usage=(40, 2))
    runtime = runtime_with(ScriptedModel(empty))
    with pytest.raises(ModelGatewayError):
        runtime.run_agent("hello")
    row = only_row(runtime)
    assert row.state is UsageState.SETTLED
    assert (row.input_tokens, row.output_tokens) == (40, 2)


def test_over_budget_output_is_settled_before_the_budget_check_rejects_it():
    runtime = runtime_with(ScriptedModel(None, tokens=(5, 9)))
    agent = runtime.agent_service.create_agent(
        name="Strict",
        instructions="Reply in JSON.",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )
    with pytest.raises(ModelGatewayError) as raised:
        runtime.run_agent("hello", agent_id=agent.id)
    assert raised.value.code == "agent.output_invalid"
    row = only_row(runtime)
    assert row.state is UsageState.SETTLED
    assert (row.input_tokens, row.output_tokens) == (5, 9)


def test_enforced_limit_refuses_the_call_before_any_provider_request():
    model = ScriptedModel(None)
    calls = []
    model.complete = lambda *a, **k: calls.append(1)  # must never run
    runtime = runtime_with(model)
    runtime.usage_service.policy = StaticLimitPolicy(
        LimitsConfig(mode="enforce", tokens_per_day=1, tokens_per_month=1)
    )
    with pytest.raises(ModelGatewayError) as raised:
        runtime.run_agent("hello")
    assert raised.value.code == "limit_exceeded" and not calls
    assert runtime.usage_ledger._rows == {}


def test_observe_mode_records_but_never_blocks():
    runtime = runtime_with(ScriptedModel(None))
    runtime.usage_service.policy = StaticLimitPolicy(
        LimitsConfig(mode="observe", tokens_per_day=1, tokens_per_month=1)
    )
    runtime.run_agent("hello")
    assert only_row(runtime).state is UsageState.SETTLED


def test_a_ledger_failure_never_fails_the_run():
    runtime = runtime_with(ScriptedModel(None))

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("ledger down")

    runtime.usage_service.ledger = Broken()
    assert runtime.run_agent("hello").agent_run.input_tokens == 11


def test_usage_settings_come_from_the_limits_config():
    runtime = LocalPlatformRuntime()
    assert runtime.usage_settings == UsageSettings(
        chars_per_token=LimitsConfig().reservation_input_chars_per_token,
        retention_days=LimitsConfig().usage_retention_days,
    )
