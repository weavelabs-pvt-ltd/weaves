"""Phase 5.0: environment parsing, bounds and the static limit policy."""

import dataclasses

import pytest

from application.ports.limits import StaticLimitPolicy, limit_policy_from_env
from shared.limits_config import LimitsConfig, LimitsConfigError


def test_defaults_match_the_design_and_start_in_observe_mode() -> None:
    config = LimitsConfig.from_env({})
    assert config == LimitsConfig()
    assert config.mode == "observe"
    assert (config.worker_slots, config.max_running_per_org) == (4, 2)
    assert (config.max_queued_per_org, config.global_max_queued) == (50, 500)
    assert config.admissions_paused is False
    assert (config.requests_per_minute_org, config.requests_per_minute_principal) == (
        600,
        240,
    )
    assert config.request_burst == 100
    assert (config.seats, config.schedules, config.agents, config.workflows) == (
        25,
        20,
        50,
        50,
    )
    assert config.tool_calls_per_day == 2_000
    assert (config.tokens_per_day, config.tokens_per_month) == (2_000_000, 40_000_000)
    assert config.est_cost_usd_per_day == 25.0
    assert config.storage_tasks == 10_000
    assert config.reservation_input_chars_per_token == 3
    assert config.queue_age_warn_seconds == 900
    assert (config.page_default, config.page_max) == (50, 100)
    assert config.recovery_batch == 200
    assert config.usage_retention_days == 400


def test_every_field_has_an_environment_setting() -> None:
    env = {
        "WEAVES_LIMITS_MODE": "ENFORCE",
        "WEAVES_WORKER_SLOTS": "2",
        "WEAVES_FAIR_CLAIM": "false",
        "WEAVES_LIMIT_MAX_RUNNING_PER_ORG": "1",
        "WEAVES_LIMIT_MAX_QUEUED_PER_ORG": "7",
        "WEAVES_LIMIT_GLOBAL_MAX_QUEUED": "70",
        "WEAVES_ADMISSIONS_PAUSED": "true",
        "WEAVES_LIMIT_REQUESTS_PER_MINUTE": "11",
        "WEAVES_LIMIT_REQUESTS_PER_MINUTE_PER_PRINCIPAL": "12",
        "WEAVES_LIMIT_REQUEST_BURST": "13",
        "WEAVES_LIMIT_SEATS": "3",
        "WEAVES_LIMIT_SCHEDULES": "4",
        "WEAVES_LIMIT_AGENTS": "5",
        "WEAVES_LIMIT_WORKFLOWS": "6",
        "WEAVES_LIMIT_TOOL_CALLS_PER_DAY": "8",
        "WEAVES_LIMIT_TOKENS_PER_DAY": "9",
        "WEAVES_LIMIT_TOKENS_PER_MONTH": "10",
        "WEAVES_LIMIT_EST_COST_USD_PER_DAY": "1.5",
        "WEAVES_LIMIT_STORAGE_TASKS": "14",
        "WEAVES_USAGE_RESERVATION_INPUT_CHARS_PER_TOKEN": "4",
        "WEAVES_QUEUE_AGE_WARN_SECONDS": "15",
        "WEAVES_PAGE_DEFAULT": "16",
        "WEAVES_PAGE_MAX": "17",
        "WEAVES_RECOVERY_BATCH": "18",
        "WEAVES_USAGE_RETENTION_DAYS": "19",
    }
    config = LimitsConfig.from_env(env)
    default = LimitsConfig()
    changed = {
        field.name
        for field in dataclasses.fields(config)
        if getattr(config, field.name) != getattr(default, field.name)
    }
    assert changed == {field.name for field in dataclasses.fields(config)}
    assert config.mode == "enforce"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WEAVES_LIMITS_MODE", "strict"),
        ("WEAVES_WORKER_SLOTS", "0"),
        ("WEAVES_WORKER_SLOTS", "33"),
        ("WEAVES_LIMIT_MAX_QUEUED_PER_ORG", "many"),
        ("WEAVES_LIMIT_SEATS", "-1"),
        ("WEAVES_ADMISSIONS_PAUSED", "maybe"),
        ("WEAVES_LIMIT_EST_COST_USD_PER_DAY", "nan"),
        ("WEAVES_LIMIT_EST_COST_USD_PER_DAY", "-3"),
        ("WEAVES_USAGE_RESERVATION_INPUT_CHARS_PER_TOKEN", "0"),
        ("WEAVES_PAGE_MAX", "5000"),
    ],
)
def test_invalid_values_are_rejected(name: str, value: str) -> None:
    with pytest.raises(LimitsConfigError, match=name):
        LimitsConfig.from_env({name: value})


def test_cross_field_relations_are_enforced() -> None:
    with pytest.raises(LimitsConfigError, match="PAGE_DEFAULT"):
        LimitsConfig.from_env({"WEAVES_PAGE_DEFAULT": "90", "WEAVES_PAGE_MAX": "80"})
    with pytest.raises(LimitsConfigError, match="MAX_RUNNING"):
        LimitsConfig.from_env({"WEAVES_LIMIT_MAX_QUEUED_PER_ORG": "1"})
    with pytest.raises(LimitsConfigError, match="TOKENS_PER_DAY"):
        LimitsConfig.from_env({"WEAVES_LIMIT_TOKENS_PER_DAY": "50000000"})


def test_blank_values_use_defaults_and_booleans_parse() -> None:
    config = LimitsConfig.from_env(
        {
            "WEAVES_LIMITS_MODE": " ",
            "WEAVES_LIMIT_SEATS": "",
            "WEAVES_LIMIT_TOKENS_PER_DAY": "1,500,000",
            "WEAVES_ADMISSIONS_PAUSED": " Yes ",
        }
    )
    assert config.mode == "observe"
    assert config.seats == 25
    assert config.tokens_per_day == 1_500_000
    assert config.admissions_paused is True
    assert (
        LimitsConfig.from_env({"WEAVES_ADMISSIONS_PAUSED": "0"}).admissions_paused
        is False
    )


def test_policy_returns_the_same_limits_for_every_organization() -> None:
    policy = limit_policy_from_env({"WEAVES_LIMIT_SEATS": "9"})
    assert isinstance(policy, StaticLimitPolicy)
    first, second = policy.for_org("org_a"), policy.for_org("org_b")
    assert first == second
    assert first.seats == 9
    assert first.mode == "observe"
    assert first.active and not first.enforcing


def test_mode_flags() -> None:
    off = limit_policy_from_env({"WEAVES_LIMITS_MODE": "off"}).for_org("o")
    enforce = limit_policy_from_env({"WEAVES_LIMITS_MODE": "enforce"}).for_org("o")
    assert not off.active and not off.enforcing
    assert enforce.active and enforce.enforcing
