"""The Slack Ask planner's usage row (``{run key}:plan``) beside the run's own row.

The planner runs before the agent run exists, so its ledger key derives from the
turn's run key. The ledger has one row per ``(org, reservation_key)``; these tests
pin that the planner row and the answer row are independent, that every operation
stays idempotent for both, that a key never exceeds the text column's contract,
and that the reconciler never settles a planner row from an agent run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from application.runtime_constants import STALE_TASK_TURN_AFTER_SECONDS
from application.task_run_keys import (
    PLANNER_KEY_SUFFIX,
    planner_usage_key,
    task_run_key,
    task_turn_id_from_run_key,
)
from contracts.v1 import UsageEvent, UsageGroupBy, UsageState
from infrastructure.persistence.usage_ledger_memory import InMemoryUsageLedger
from modules.usage.reconciler import PLANNER_GRACE
from tests.contracts.test_usage_ledger import reservation, settlement
from tests.support.runtime import LocalPlatformRuntime

NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
DAY = date(2026, 11, 24)
RUN_KEY = task_run_key("0b0c5f0e-1111-4222-8333-444455556666", 1)


def test_the_planner_key_is_the_run_key_plus_plan():
    assert planner_usage_key(RUN_KEY) == RUN_KEY + PLANNER_KEY_SUFFIX
    assert planner_usage_key(RUN_KEY).endswith(":plan")
    # It keeps the turn id, so the planner row joins to its own task turn.
    assert task_turn_id_from_run_key(planner_usage_key(RUN_KEY)) == (
        task_turn_id_from_run_key(RUN_KEY)
    )


@pytest.mark.parametrize("length", [10, 100, 117, 122, 123, 128])
def test_a_planner_key_always_fits_the_128_character_column_contract(length):
    run_key = "r" * length
    key = planner_usage_key(run_key)
    assert len(key) <= 128 and key.endswith(":plan")
    UsageEvent(  # the contract that bounds every ledger key
        org_id="org-a",
        workspace_id="ws",
        reservation_key=key,
        agent_run_id=key,
        run_id=key,
        agent_id="agent-1",
        state=UsageState.RESERVED,
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_at=NOW,
    )


def test_long_run_keys_stay_distinct_after_shortening():
    first = planner_usage_key("x" * 127 + "a")
    second = planner_usage_key("x" * 127 + "b")
    assert first != second and len(first) <= 128 and len(second) <= 128


def test_planner_and_answer_rows_are_independent_and_each_settles_once():
    ledger = InMemoryUsageLedger()
    plan_key = planner_usage_key("run-1")
    assert ledger.reserve(reservation("run-1"), None).created
    assert ledger.reserve(reservation(plan_key, tokens=(500, 120)), None).created
    assert not ledger.reserve(reservation(plan_key), None).created  # replay
    assert ledger.settle(settlement(plan_key, tokens=(480, 40))) is True
    assert ledger.settle(settlement(plan_key, tokens=(999, 999))) is False
    assert ledger.get("org-a", "run-1").state is UsageState.RESERVED
    assert ledger.settle(settlement("run-1", tokens=(1000, 200))) is True
    assert ledger.settle(settlement("run-1", tokens=(1, 1))) is False
    totals, _, unknown, in_flight = ledger.summary(
        "org-a", DAY, DAY, UsageGroupBy.AGENT
    )
    # Two calls, each counted once: the planner's tokens add to the run's.
    assert (totals.calls, totals.input_tokens, totals.output_tokens) == (2, 1480, 240)
    assert (unknown, in_flight) == (0, 0)


def test_a_late_planner_settlement_upgrades_unknown_exactly_once():
    ledger = InMemoryUsageLedger()
    key = planner_usage_key("run-2")
    ledger.reserve(reservation(key), None)
    assert ledger.mark_unknown("org-a", key, outcome="planner_expired", now=NOW)
    assert ledger.settle(settlement(key, tokens=(300, 30))) is True
    assert ledger.settle(settlement(key, tokens=(300, 30))) is False
    totals, _, unknown, _ = ledger.summary("org-a", DAY, DAY, UsageGroupBy.AGENT)
    assert (totals.calls, totals.input_tokens) == (1, 300) and unknown == 0


def test_the_reconciler_waits_for_a_live_planner_call_then_marks_it_unknown():
    runtime = LocalPlatformRuntime()
    try:
        runtime.bootstrap()
        ledger = runtime.usage_service.ledger
        key = planner_usage_key("run-3")
        fresh = datetime.now(UTC)
        ledger.reserve(reservation(key, org="local-org", now=fresh), None)
        assert runtime.usage_reconciler.reconcile() == 0  # a call in flight
        assert ledger.get("local-org", key).state is UsageState.RESERVED
        old = fresh - PLANNER_GRACE - timedelta(seconds=1)
        stale_key = planner_usage_key("run-4")
        ledger.reserve(reservation(stale_key, org="local-org", now=old), None)
        assert runtime.usage_reconciler.reconcile() == 1
        row = ledger.get("local-org", stale_key)
        assert row.state is UsageState.UNKNOWN and row.outcome == "planner_expired"
        assert ledger.get("local-org", key).state is UsageState.RESERVED
        # The planner's grace is far below the turn lease fence it replaces.
        assert PLANNER_GRACE.total_seconds() < STALE_TASK_TURN_AFTER_SECONDS
    finally:
        runtime.close()
