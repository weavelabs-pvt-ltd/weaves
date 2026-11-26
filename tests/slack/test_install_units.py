"""Pure pieces of the install and events slices."""

from __future__ import annotations

import pytest

from modules.slack.ingest_limiter import IngestLimiter
from modules.slack.install_settings import (
    BOT_SCOPES,
    REQUIRED_SCOPES,
    SlackInstallSettings,
)
from modules.slack.installation_payload import InstallRefused, parse_install
from tests.slack.support import BOT_TOKEN, access_payload


def test_the_bot_scopes_are_exactly_the_design_list_and_no_user_scope() -> None:
    assert set(BOT_SCOPES) == {
        "app_mentions:read",
        "chat:write",
        "channels:history",
        "groups:history",
        "channels:read",
        "groups:read",
        "reactions:read",
        "reactions:write",
    }
    assert set(BOT_SCOPES) >= REQUIRED_SCOPES
    assert not any(scope.startswith("search:") for scope in BOT_SCOPES)


def test_a_valid_install_is_parsed_and_the_token_never_prints() -> None:
    install = parse_install(access_payload())
    assert (install.team_id, install.bot_user_id) == ("T0ACME001", "U0BOT0001")
    assert install.slack_user == "U0ADMIN01" and install.enterprise_id is None
    assert set(install.scopes) == set(BOT_SCOPES)
    assert BOT_TOKEN not in repr(install)


def test_a_per_workspace_grid_install_keeps_its_enterprise_id() -> None:
    payload = access_payload()
    payload["enterprise"] = {"id": "E0GRID001"}
    assert parse_install(payload).enterprise_id == "E0GRID001"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"ok": False}, "provider_response"),
        ({"access_token": None}, "provider_response"),
        ({"bot_user_id": ""}, "provider_response"),
        ({"is_enterprise_install": True}, "enterprise_install_unsupported"),
        ({"team": None}, "enterprise_install_unsupported"),
        ({"team": {"name": "no id"}}, "enterprise_install_unsupported"),
        ({"scope": "chat:write"}, "scopes_missing"),
        ({"scope": None}, "scopes_missing"),
    ],
)
def test_an_unusable_answer_is_refused_with_a_code(change: dict, reason: str) -> None:
    with pytest.raises(InstallRefused) as refused:
        parse_install({**access_payload(), **change})
    assert refused.value.reason == reason


def test_settings_need_every_piece_before_an_install_can_start() -> None:
    full = {
        "WEAVES_SLACK_ENABLED": "true",
        "WEAVES_SLACK_SIGNING_SECRET": "s",
        "WEAVES_SLACK_OAUTH_CLIENT_ID": "id",
        "WEAVES_SLACK_OAUTH_CLIENT_SECRET": "secret",
        "WEAVES_PUBLIC_API_BASE_URL": "https://api.example.test/",
    }
    ready = SlackInstallSettings.from_environment(full)
    assert ready.install_available
    assert ready.redirect_uri == (
        "https://api.example.test/api/v0/oauth/slack/install/callback"
    )
    assert "secret" not in repr(ready).replace("signing_secret", "")
    for missing in full:
        partial = {k: v for k, v in full.items() if k != missing}
        if missing in {"WEAVES_SLACK_ENABLED", "WEAVES_SLACK_SIGNING_SECRET"}:
            partial["WEAVES_SLACK_ENABLED"] = "false"
        assert not SlackInstallSettings.from_environment(partial).install_available


def test_the_limiter_refills_and_refuses_a_flood() -> None:
    now = [0.0]
    limiter = IngestLimiter(rate_per_second=2, burst=3, clock=lambda: now[0])
    assert [limiter.acquire() for _ in range(4)] == [True, True, True, False]
    now[0] = 1.0
    assert [limiter.acquire() for _ in range(3)] == [True, True, False]
