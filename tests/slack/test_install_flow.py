"""The bot install: start, callback, status, reinstall and disconnect."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.slack.install_settings import BOT_SCOPES
from shared.request_context import reset_request_principal, set_request_identity
from tests.slack.support import (
    BOT_TOKEN,
    CALLBACK,
    START,
    STATUS,
    World,
    access_payload,
    running_world,
)


def _redirect(response: Any) -> dict[str, str]:
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://app.example.test/?")
    assert location.endswith("#connections")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def _state(world: World, path: str = START) -> str:
    started = world.client.get(path, headers=world.alpha.headers)
    assert started.status_code == 200, started.text
    return parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]


def _live_secrets(world: World, refs: list[str | None]) -> list[str | None]:
    token = set_request_identity("p", world.alpha.org_id, world.alpha.workspace_id)
    try:
        live: list[str | None] = []
        for ref in refs:
            assert ref is not None
            try:
                world.runtime.secret_store.get(ref)
                live.append(ref)
            except KeyError:
                pass
        return live
    finally:
        reset_request_principal(token)


def test_start_returns_the_bot_authorize_url_with_only_bot_scopes(world: World) -> None:
    response = world.client.get(START, headers=world.alpha.headers)
    assert response.status_code == 200
    url = urlsplit(response.json()["authorization_url"])
    query = parse_qs(url.query)
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "slack.com",
        "/oauth/v2/authorize",
    )
    assert query["scope"] == [",".join(BOT_SCOPES)]
    assert "user_scope" not in query
    assert query["client_id"] == ["client-id-1"]
    assert query["redirect_uri"] == [
        "https://api.example.test/api/v0/oauth/slack/install/callback"
    ]
    assert len(query["state"][0]) >= 32
    assert "client-secret-1" not in response.text


def test_install_binds_the_workspace_and_never_exposes_the_token(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert _redirect(world.install()) == {"slack_install": "ok"}
    view = world.status()
    assert view["available"] is True
    (installation,) = view["installations"]
    assert installation["team_id"] == "T0ACME001"
    assert installation["team_name"] == "Acme Workspace"
    assert installation["status"] == "active"
    assert installation["installed_by"] == "Owner"
    assert installation["missing_scopes"] == []
    assert installation["needs_reinstall"] is False
    stored = world.runtime.slack_store.find_by_team("T0ACME001")
    assert stored is not None and stored.org_id == world.alpha.org_id
    assert stored.bot_secret_ref and BOT_TOKEN not in repr(stored)
    assert BOT_TOKEN not in json.dumps(view)
    product_logs = " ".join(
        record.getMessage() for record in caplog.records if "product" in record.name
    )
    assert BOT_TOKEN not in product_logs and "code-1" not in product_logs
    assert world.api.methods() == ["oauth.v2.access"]  # no token used or revoked


def test_the_install_is_audited_without_secrets(world: World) -> None:
    world.install()
    events = [
        e
        for e in world.runtime.audit_events.list_scoped(world.alpha.org_id)
        if e.action.startswith("slack.installation")
    ]
    assert [e.action for e in events] == ["slack.installation.created"]
    assert events[0].metadata["team_id"] == "T0ACME001"
    assert BOT_TOKEN not in json.dumps(events[0].model_dump(mode="json"))


def test_state_is_single_use(world: World) -> None:
    state = _state(world)
    first = world.client.get(
        CALLBACK, params={"code": "c1", "state": state}, follow_redirects=False
    )
    assert _redirect(first) == {"slack_install": "ok"}
    replay = world.client.get(
        CALLBACK, params={"code": "c1", "state": state}, follow_redirects=False
    )
    assert _redirect(replay) == {
        "slack_install": "failed",
        "slack_error": "invalid_state",
    }
    assert world.api.methods() == ["oauth.v2.access"]  # no second exchange


def test_an_unknown_state_or_a_denial_exchanges_nothing(world: World) -> None:
    bad = world.client.get(
        CALLBACK, params={"code": "c", "state": "nope"}, follow_redirects=False
    )
    assert _redirect(bad)["slack_error"] == "invalid_state"
    denied = world.client.get(
        CALLBACK,
        params={"error": "access_denied", "state": _state(world)},
        follow_redirects=False,
    )
    assert _redirect(denied)["slack_error"] == "authorization_denied"
    assert world.api.calls == []
    assert world.status()["installations"] == []


def test_a_connector_state_cannot_complete_a_bot_install(world: World) -> None:
    other = world.client.post(
        "/api/v0/oauth/slack/start",
        headers=world.alpha.headers,
        json={"display_name": "Search"},
    )
    assert other.status_code == 200, other.text
    state = parse_qs(urlsplit(other.json()["authorization_url"]).query)["state"][0]
    response = world.client.get(
        CALLBACK, params={"code": "c", "state": state}, follow_redirects=False
    )
    assert _redirect(response)["slack_error"] == "invalid_state"
    assert world.api.calls == []


def test_a_second_organization_cannot_claim_a_bound_workspace(world: World) -> None:
    assert _redirect(world.install(world.alpha)) == {"slack_install": "ok"}
    world.api.calls.clear()
    refused = _redirect(world.install(world.beta, code="code-2"))
    assert refused == {
        "slack_install": "failed",
        "slack_error": "workspace_already_connected",
    }
    # The token Slack just issued for the refused install is revoked at once.
    assert world.api.methods() == ["oauth.v2.access", "auth.revoke"]
    assert world.status(world.beta)["installations"] == []
    holder = world.runtime.slack_store.find_by_team("T0ACME001")
    assert holder is not None and holder.org_id == world.alpha.org_id


def test_reinstalling_by_the_same_organization_rotates_the_token(world: World) -> None:
    world.install()
    before = world.runtime.slack_store.find_by_team("T0ACME001")
    world.api.access = access_payload(token="xoxb-second-token")
    assert _redirect(world.install(code="code-2")) == {"slack_install": "ok"}
    after = world.runtime.slack_store.find_by_team("T0ACME001")
    assert after is not None and before is not None
    assert after.installation_id == before.installation_id  # updated in place
    assert after.bot_secret_ref != before.bot_secret_ref
    assert len(world.status()["installations"]) == 1
    assert _live_secrets(world, [before.bot_secret_ref]) == []
    assert _live_secrets(world, [after.bot_secret_ref]) == [after.bot_secret_ref]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (access_payload(enterprise_install=True), "enterprise_install_unsupported"),
        (access_payload(scope="chat:write"), "scopes_missing"),
        (
            {"ok": True, "access_token": "xoxb-x", "team": {"id": "T1"}},
            "provider_response",
        ),
    ],
)
def test_unusable_installs_are_refused_and_issued_tokens_revoked(
    world: World, payload: dict[str, Any], reason: str
) -> None:
    world.api.access = payload
    assert _redirect(world.install()) == {
        "slack_install": "failed",
        "slack_error": reason,
    }
    assert world.status()["installations"] == []
    revoked = ["auth.revoke"] if payload.get("access_token") else []
    assert world.api.methods() == ["oauth.v2.access", *revoked]


def test_a_failed_exchange_is_a_clean_failure(world: World) -> None:
    world.api.errors["oauth.v2.access"] = "invalid_code"
    assert _redirect(world.install())["slack_error"] == "token_exchange"
    assert world.status()["installations"] == []


def test_disconnect_revokes_deletes_the_token_and_audits(world: World) -> None:
    world.install()
    installation = world.status()["installations"][0]
    stored = world.runtime.slack_store.find_by_team("T0ACME001")
    assert stored is not None
    world.api.calls.clear()
    world.api.tokens.clear()
    response = world.client.delete(
        f"{STATUS}/{installation['installation_id']}", headers=world.alpha.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "uninstalled"
    assert world.api.methods() == ["auth.revoke"]
    assert world.api.tokens == [BOT_TOKEN]  # revoked with the stored token
    assert _live_secrets(world, [stored.bot_secret_ref]) == []
    assert world.runtime.slack_store.find_by_team("T0ACME001") is None
    actions = [
        e.action
        for e in world.runtime.audit_events.list_scoped(world.alpha.org_id)
        if e.action.startswith("slack.installation")
    ]
    assert actions == ["slack.installation.created", "slack.installation.disconnected"]
    # The workspace is free again: another organization may now claim it.
    assert _redirect(world.install(world.beta, code="code-3")) == {
        "slack_install": "ok"
    }


def test_disconnect_survives_a_failing_slack_revoke(world: World) -> None:
    world.install()
    world.api.errors["auth.revoke"] = "token_revoked"
    installation = world.status()["installations"][0]
    response = world.client.delete(
        f"{STATUS}/{installation['installation_id']}", headers=world.alpha.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "uninstalled"


def test_the_install_is_unavailable_when_slack_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_world(
        monkeypatch, WEAVES_SLACK_ENABLED="false", WEAVES_SLACK_SIGNING_SECRET=""
    ) as dark:
        assert dark.status()["available"] is False
        refused = dark.client.get(START, headers=dark.alpha.headers)
        assert refused.status_code == 503
        assert dark.api.calls == []
