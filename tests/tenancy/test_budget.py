"""Test the lifetime cap before any real paid model request is permitted."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from tenancy_harness.budget import (
    MAX_OUTPUT_TOKENS,
    BudgetExhausted,
    Ledger,
    initialize,
    usage_cost,
)


def config(limit=100_000_000_000):
    return {
        "lifetime_limit_nanos": limit,
        "request_reservation_nanos": 1_000_000_000,
        "rates_verified_at": datetime.now(UTC).isoformat(),
        "accounting_multiplier": "2",
        "rates": {
            "model": {"input_per_million_usd": "2", "output_per_million_usd": "10"}
        },
    }


def test_budget_survives_restarts_and_refuses_automatic_reset(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    ledger.reserve("model", "run-a", 3)
    assert Ledger(tmp_path).snapshot()["available_usd"] == "99"
    with pytest.raises(ValueError, match="preserved"):
        initialize(tmp_path, config())


def test_concurrent_admissions_cannot_exceed_lifetime_cap(tmp_path):
    initialize(tmp_path, config(2_000_000_000))

    def attempt(i):
        try:
            Ledger(tmp_path).reserve("model", str(i), 3)
            return True
        except BudgetExhausted:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 2
    assert Ledger(tmp_path).snapshot()["available_usd"] == "0"


def test_settlement_releases_only_unused_hold_and_cannot_be_replayed(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    request = ledger.reserve("model", "run", 3)
    ledger.settle(request, "model", {"input_tokens": 1000, "output_tokens": 100})
    assert ledger.snapshot()["accounted_usd"] == "0.006"
    assert ledger.snapshot()["available_usd"] == "99.994"
    with pytest.raises(ValueError, match="settled"):
        ledger.settle(request, "model", {"input_tokens": 0, "output_tokens": 0})


def test_unknown_outcome_retains_its_full_reservation(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    request = ledger.reserve("model", "run", 3)
    ledger.uncertain(request)
    assert Ledger(tmp_path).snapshot()["unresolved_reserved_usd"] == "1"
    assert Ledger(tmp_path).snapshot()["available_usd"] == "99"


def test_unknown_price_expired_price_and_per_run_limit_block_calls(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    with pytest.raises(ValueError, match="Unknown model"):
        ledger.reserve("unknown", "run", 3)
    for _ in range(3):
        ledger.reserve("model", "run", 3)
    with pytest.raises(BudgetExhausted, match="Per-run"):
        ledger.reserve("model", "run", 3)
    ledger.config["rates_verified_at"] = (
        datetime.now(UTC) - timedelta(days=15)
    ).isoformat()
    with pytest.raises(ValueError, match="prices"):
        ledger.reserve("model", "other", 3)


def test_unpriced_usage_stays_reserved_and_missing_ledger_cannot_reset(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    request = ledger.reserve("model", "run", 3)
    with pytest.raises(ValueError, match="Unpriced"):
        ledger.settle(
            request,
            "model",
            {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 10},
        )
    assert ledger.snapshot()["available_usd"] == "99"
    ledger.path.unlink()
    with pytest.raises(RuntimeError, match="reset"):
        Ledger(tmp_path)


def test_request_hold_must_cover_worst_case_before_any_paid_call(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    ledger.config["rates"]["model"]["input_per_million_usd"] = "1000"
    with pytest.raises(ValueError, match="maximum possible cost"):
        ledger.reserve("model", "run", 3)
    assert ledger.snapshot()["requests"] == 0


def test_invalid_prices_or_discounting_margin_cannot_bypass_the_cap(tmp_path):
    initialize(tmp_path, config())
    ledger = Ledger(tmp_path)
    ledger.config["accounting_multiplier"] = "0"
    with pytest.raises(ValueError, match="margin"):
        ledger.reserve("model", "run", 3)
    assert ledger.snapshot()["requests"] == 0


def test_thinking_and_text_maximum_still_fits_the_unchanged_reservation(tmp_path):
    assert MAX_OUTPUT_TOKENS == 1024
    settings = config()
    cost = usage_cost(
        settings, "model", {"input_tokens": 64000, "output_tokens": MAX_OUTPUT_TOKENS}
    )
    assert cost == 276_480_000
    assert cost < settings["request_reservation_nanos"]
    initialize(tmp_path, settings)
    ledger = Ledger(tmp_path)
    request = ledger.reserve("model", "bounded-thinking", 3)
    ledger.settle(
        request, "model", {"input_tokens": 64000, "output_tokens": MAX_OUTPUT_TOKENS}
    )
    assert ledger.snapshot()["lifetime_limit_usd"] == "100"
