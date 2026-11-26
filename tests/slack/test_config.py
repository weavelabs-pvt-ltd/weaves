"""Slack settings: optional, dark by default, secret never printed."""

import pytest

from shared.slack_config import DEFAULT_API_BASE_URL, SlackConfig, SlackConfigError

MADE_UP_SECRET = "made-up-signing-secret-for-tests"


def test_absent_environment_leaves_the_bot_off() -> None:
    config = SlackConfig.from_environment({})
    assert config.enabled is False
    assert config.signing_secret == b""
    assert config.api_base_url == DEFAULT_API_BASE_URL == "https://slack.com/api"


def test_values_are_read_and_trimmed() -> None:
    config = SlackConfig.from_environment(
        {
            "WEAVES_SLACK_SIGNING_SECRET": f"  {MADE_UP_SECRET} ",
            "WEAVES_SLACK_ENABLED": "TRUE",
            "WEAVES_SLACK_API_BASE_URL": "http://provider:8081/slack/api/",
        }
    )
    assert config.signing_secret == MADE_UP_SECRET.encode()
    assert config.enabled is True
    assert config.api_base_url == "http://provider:8081/slack/api"


def test_enabled_without_a_secret_fails_at_start() -> None:
    with pytest.raises(SlackConfigError, match="SIGNING_SECRET"):
        SlackConfig.from_environment({"WEAVES_SLACK_ENABLED": "true"})


def test_secret_without_enabled_is_allowed_but_stays_off() -> None:
    config = SlackConfig.from_environment({"WEAVES_SLACK_SIGNING_SECRET": "x"})
    assert config.enabled is False


@pytest.mark.parametrize("value", ["maybe", "2"])
def test_malformed_flag_is_rejected(value: str) -> None:
    with pytest.raises(SlackConfigError):
        SlackConfig.from_environment({"WEAVES_SLACK_ENABLED": value})


@pytest.mark.parametrize("value", ["slack.com/api", "ftp://slack.com/api", "https://"])
def test_malformed_api_base_url_is_rejected(value: str) -> None:
    with pytest.raises(SlackConfigError):
        SlackConfig.from_environment({"WEAVES_SLACK_API_BASE_URL": value})


def test_repr_and_str_never_show_the_secret() -> None:
    config = SlackConfig.from_environment(
        {"WEAVES_SLACK_SIGNING_SECRET": MADE_UP_SECRET, "WEAVES_SLACK_ENABLED": "1"}
    )
    assert MADE_UP_SECRET not in repr(config)
    assert MADE_UP_SECRET not in str(config)
    assert "signing_secret" not in repr(config)


def test_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "yes")
    monkeypatch.setenv("WEAVES_SLACK_SIGNING_SECRET", MADE_UP_SECRET)
    assert SlackConfig.from_environment().enabled is True
