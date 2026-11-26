"""Org state and lifecycle events: what is stored, dropped or ends the install."""

from __future__ import annotations

import pytest

from tests.slack.support import (
    World,
    forbid_database,
    lifecycle_event,
    mention,
    running_world,
)


@pytest.fixture
def installed(world: World) -> World:
    world.install()
    world.api.calls.clear()
    return world


def _inbox(world: World) -> list:
    return world.runtime.slack_store.list_events()


def test_a_suspended_or_quarantined_organization_stores_no_text(
    installed: World,
) -> None:
    installed.runtime.organization_lifecycle_service.suspend(
        installed.alpha.org_id, "ops@weaves.test", "quarantine for test"
    )
    assert installed.deliver(mention()).status_code == 200
    (row,) = _inbox(installed)
    assert (row.state, row.drop_reason, row.text) == (
        "dropped",
        "org_unavailable",
        None,
    )
    assert installed.api.calls == []  # no acknowledgement either


def test_a_dropped_event_is_still_deduplicated(installed: World) -> None:
    installed.runtime.organization_lifecycle_service.suspend(
        installed.alpha.org_id, "ops@weaves.test", "pause"
    )
    installed.deliver(mention())
    installed.runtime.organization_lifecycle_service.resume(
        installed.alpha.org_id, "ops@weaves.test", "resume"
    )
    installed.deliver(mention(), extra={"x-slack-retry-num": "1"})
    assert [r.state for r in _inbox(installed)] == ["dropped"]  # not revived


def test_an_organization_resumed_later_accepts_new_events(installed: World) -> None:
    lifecycle = installed.runtime.organization_lifecycle_service
    lifecycle.suspend(installed.alpha.org_id, "ops@weaves.test", "pause")
    lifecycle.resume(installed.alpha.org_id, "ops@weaves.test", "resume")
    installed.deliver(mention())
    assert [r.state for r in _inbox(installed)] == ["pending"]


def test_paused_platform_admissions_drop_the_event(
    installed: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = installed.runtime.platform_controls
    snapshot = controls.snapshot()
    monkeypatch.setattr(
        controls,
        "snapshot",
        lambda: type(snapshot)(**{**vars(snapshot), "admissions_paused": True}),
    )
    assert installed.deliver(mention()).status_code == 200
    (row,) = _inbox(installed)
    assert (row.state, row.drop_reason, row.text) == (
        "dropped",
        "platform_paused",
        None,
    )


def test_a_suspended_installation_drops_the_event(installed: World) -> None:
    store = installed.runtime.slack_store
    (view,) = installed.status()["installations"]
    from datetime import UTC, datetime

    from shared.request_context import reset_request_principal, set_request_identity

    token = set_request_identity(
        "p", installed.alpha.org_id, installed.alpha.workspace_id
    )
    try:
        store.set_status(view["installation_id"], "suspended", datetime.now(UTC))
    finally:
        reset_request_principal(token)
    installed.deliver(mention())
    (row,) = _inbox(installed)
    assert (row.state, row.drop_reason) == ("dropped", "installation_suspended")


def test_the_kill_switch_acks_and_drops_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_world(monkeypatch) as live:
        live.install()
    with running_world(monkeypatch, WEAVES_SLACK_ENABLED="false") as dark:
        forbid_database(dark, monkeypatch)  # not even a lookup
        response = dark.deliver(mention())
        assert response.status_code == 200
        assert _inbox(dark) == [] and dark.api.calls == []
        challenge = dark.deliver({"type": "url_verification", "challenge": "kept"})
        assert challenge.json() == {"challenge": "kept"}  # the URL still verifies


def test_app_uninstalled_ends_the_installation_and_deletes_the_token(
    installed: World,
) -> None:
    assert installed.deliver(lifecycle_event("app_uninstalled")).status_code == 200
    (view,) = installed.status()["installations"]
    assert view["status"] == "uninstalled"
    assert installed.runtime.slack_store.find_by_team("T0ACME001") is None
    actions = [
        e.action
        for e in installed.runtime.audit_events.list_scoped(installed.alpha.org_id)
        if e.action.startswith("slack.installation")
    ]
    assert actions[-1] == "slack.installation.uninstalled"
    # Dispatch has stopped, the repeat is harmless and nothing was sent to Slack.
    assert installed.deliver(lifecycle_event("app_uninstalled")).status_code == 200
    assert installed.deliver(mention()).status_code == 200
    assert _inbox(installed) == [] and installed.api.calls == []


def test_a_bot_token_revocation_marks_the_installation_revoked(
    installed: World,
) -> None:
    event = lifecycle_event(
        "tokens_revoked", tokens={"oauth": [], "bot": ["U0BOT0001"]}
    )
    assert installed.deliver(event).status_code == 200
    (view,) = installed.status()["installations"]
    assert (view["status"], view["needs_reinstall"]) == ("revoked", True)
    assert installed.api.calls == []


def test_revoking_only_user_tokens_leaves_the_bot_alone(installed: World) -> None:
    event = lifecycle_event(
        "tokens_revoked", tokens={"oauth": ["U0ADMIN01"], "bot": []}
    )
    assert installed.deliver(event).status_code == 200
    assert installed.status()["installations"][0]["status"] == "active"


def test_a_revoked_workspace_can_be_reinstalled_in_place(installed: World) -> None:
    event = lifecycle_event("tokens_revoked", tokens={"bot": ["U0BOT0001"]})
    installed.deliver(event)
    before = installed.status()["installations"][0]["installation_id"]
    installed.install(code="code-9")
    (view,) = installed.status()["installations"]
    assert (view["installation_id"], view["status"]) == (before, "active")


def test_a_lifecycle_event_for_an_unknown_team_changes_nothing(
    installed: World,
) -> None:
    event = lifecycle_event("app_uninstalled", team="T0STRANGER")
    assert installed.deliver(event).status_code == 200
    assert installed.status()["installations"][0]["status"] == "active"
