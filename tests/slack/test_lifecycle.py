"""S7: one path ends an installation, in a fail-closed order, with nothing leaked."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from modules.auth.organization_export_rules import EXPORT_COLLECTIONS
from modules.recovery.tombstones import FileTombstoneLog, TombstoneWriteError
from shared.request_context import reset_request_principal, set_request_identity
from tests.slack.support import (
    BOT_TOKEN,
    World,
    access_payload,
    lifecycle_event,
    mention,
    running_world,
)

TEAM = "T0ACME001"


@pytest.fixture
def tombstoned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[World, FileTombstoneLog]]:
    """A memory world whose lifecycle writes a real tombstone log."""
    monkeypatch.setenv("WEAVES_TOMBSTONE_DIR", str(tmp_path / "tombstones"))
    with running_world(monkeypatch) as live:
        live.install()
        live.api.calls.clear()
        yield live, FileTombstoneLog(tmp_path / "tombstones")


def kinds(log: FileTombstoneLog) -> list[str]:
    return [t.kind for t in log.read()]


def owner_headers(world: World, org) -> dict[str, str]:
    bearer = org.headers["Authorization"]
    return {"Authorization": bearer, "X-Organization-Id": org.org_id}


def alpha_scope(world: World):
    return set_request_identity("p", world.alpha.org_id, world.alpha.workspace_id)


def test_uninstall_writes_the_installation_tombstone(
    tombstoned: tuple[World, FileTombstoneLog],
) -> None:
    world, log = tombstoned
    assert world.deliver(lifecycle_event("app_uninstalled")).status_code == 200
    ended = [t for t in log.read() if t.kind == "slack_installation_ended"]
    assert kinds(log) == ["slack_installation_ended"]
    assert (ended[0].org_id, ended[0].subject_kind) == (
        world.alpha.org_id,
        "uninstalled",
    )
    assert "@" in ended[0].subject_id  # installation id and install generation


def test_an_unwritable_tombstone_log_changes_nothing_and_slack_retries(
    tombstoned: tuple[World, FileTombstoneLog], monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _log = tombstoned

    def refuse(*_a: object, **_k: object) -> None:
        raise TombstoneWriteError("disk full")

    monkeypatch.setattr(world.runtime.slack_lifecycle.tombstones, "record", refuse)
    assert world.deliver(lifecycle_event("app_uninstalled")).status_code == 503
    (view,) = world.status()["installations"]
    assert view["status"] == "active"  # nothing was changed or deleted
    held = world.runtime.slack_store.find_by_team(TEAM)
    token = alpha_scope(world)
    try:
        assert world.runtime.secret_store.get(held.bot_secret_ref) == BOT_TOKEN
    finally:
        reset_request_principal(token)
    monkeypatch.undo()
    assert world.deliver(lifecycle_event("app_uninstalled")).status_code == 200
    assert world.status()["installations"][0]["status"] == "uninstalled"


@pytest.mark.parametrize("path", ["event", "disconnect"])
def test_disconnect_and_slack_events_share_one_code_path(
    tombstoned: tuple[World, FileTombstoneLog], path: str
) -> None:
    world, log = tombstoned
    assert world.deliver(mention()).status_code == 200
    (view,) = world.status()["installations"]
    if path == "event":
        world.deliver(lifecycle_event("app_uninstalled"))
    else:
        done = world.client.delete(
            f"/api/v0/organization/slack/{view['installation_id']}",
            headers=world.alpha.headers,
        )
        assert done.status_code == 200
    # Same effects either way: tombstones, pending row dropped without text, row
    # ended with no token reference, workspace freed, one audit event.
    assert kinds(log) == ["slack_installation_ended"]
    (row,) = world.runtime.slack_store.list_events()
    assert (row.state, row.drop_reason, row.text) == (
        "dropped",
        "installation_ended",
        None,
    )
    token = alpha_scope(world)
    try:
        (stored,) = world.runtime.slack_store.list_for_org()
        audit = [
            e
            for e in world.runtime.audit_events.list_scoped(world.alpha.org_id)
            if e.action.startswith("slack.installation.")
            and e.action != "slack.installation.created"
        ]
    finally:
        reset_request_principal(token)
    assert stored.bot_secret_ref is None and stored.status == "uninstalled"
    assert world.runtime.slack_store.find_by_team(TEAM) is None
    assert len(audit) == 1 and audit[0].metadata["pending_events_dropped"] == 1
    assert world.api.methods().count("auth.revoke") == (path == "disconnect")


def test_events_after_the_end_are_unknown_team_and_a_repeat_is_harmless(
    tombstoned: tuple[World, FileTombstoneLog],
) -> None:
    world, log = tombstoned
    world.deliver(lifecycle_event("app_uninstalled"))
    first = kinds(log)
    world.deliver(lifecycle_event("app_uninstalled"))
    world.deliver(lifecycle_event("tokens_revoked", tokens={"bot": ["U0BOT0001"]}))
    assert world.deliver(mention()).status_code == 200
    assert kinds(log) == first
    assert world.runtime.slack_store.list_events() == []  # unknown team: no row


def test_another_organization_can_install_after_an_uninstall(world: World) -> None:
    world.install()
    world.deliver(lifecycle_event("app_uninstalled"))
    assert (
        "slack_install=ok" in world.install(world.beta, code="c2").headers["location"]
    )
    token = set_request_identity("p", world.beta.org_id, world.beta.workspace_id)
    try:
        (view,) = world.runtime.slack_store.list_for_org()
    finally:
        reset_request_principal(token)
    assert view.status == "active" and view.team_id == TEAM
    assert world.status()["installations"][0]["status"] == "uninstalled"
    # Events now belong to the new organization only.
    world.deliver(mention())
    (row,) = world.runtime.slack_store.list_events()
    assert row.org_id == world.beta.org_id and row.state == "pending"


def test_the_same_organization_reinstalls_cleanly_after_a_revocation(
    world: World,
) -> None:
    world.install()
    world.deliver(lifecycle_event("tokens_revoked", tokens={"bot": ["U0BOT0001"]}))
    world.api.access = access_payload(token="xoxb-second-token-never-leaks")
    world.install(code="c3")
    assert world.status()["installations"][0]["status"] == "active"
    assert world.deliver(mention()).status_code == 200
    assert [r.state for r in world.runtime.slack_store.list_events()] == ["pending"]


def test_a_crash_after_the_flip_is_finished_by_the_sweep(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install()
    secrets = world.runtime.secret_store
    real_delete = secrets.delete

    def crash(_ref: str) -> None:
        raise RuntimeError("process died")

    monkeypatch.setattr(secrets, "delete", crash)
    assert world.deliver(lifecycle_event("app_uninstalled")).status_code == 200
    assert world.runtime.slack_store.find_by_team(TEAM) is None  # dispatch stopped
    (left,) = world.runtime.slack_store.list_unfinished()
    assert left.status == "uninstalled" and left.bot_secret_ref is not None
    monkeypatch.setattr(secrets, "delete", real_delete)
    assert world.runtime.slack_lifecycle.sweep() == 1
    assert world.runtime.slack_store.list_unfinished() == []
    assert world.runtime.slack_lifecycle.sweep() == 0  # idempotent


def test_no_token_reaches_logs_audit_or_the_tombstone_log(
    tombstoned: tuple[World, FileTombstoneLog], caplog: pytest.LogCaptureFixture
) -> None:
    world, log = tombstoned
    with caplog.at_level(logging.DEBUG):
        world.deliver(mention())
        world.deliver(lifecycle_event("app_uninstalled"))
    world.install(code="c9")
    world.api.access = access_payload(token="xoxb-another-secret-token")
    view = world.status()["installations"][0]
    world.client.delete(
        f"/api/v0/organization/slack/{view['installation_id']}",
        headers=world.alpha.headers,
    )
    token = alpha_scope(world)
    try:
        audit = json.dumps(
            [
                e.model_dump(mode="json")
                for e in world.runtime.audit_events.list_scoped(world.alpha.org_id)
            ]
        )
    finally:
        reset_request_principal(token)
    everything = audit + caplog.text + json.dumps(world.status())
    everything += (log.path).read_text()
    for secret in (BOT_TOKEN, "xoxb-another-secret-token"):
        assert secret not in everything


def test_the_installation_is_exported_without_any_token(world: World) -> None:
    from modules.auth.organization_export_rules import Withheld

    world.install()
    service = world.runtime.organization_export_service

    def document(org) -> dict:
        token = set_request_identity("p", org.org_id, org.workspace_id)
        try:
            return service._document(org.org_id, {}, Withheld("p"))
        finally:
            reset_request_principal(token)

    text = json.dumps(document(world.alpha), default=str)
    (install,) = json.loads(text)["slack_installations"]
    assert install["team_id"] == TEAM and install["status"] == "active"
    assert "bot_secret_ref" not in install and BOT_TOKEN not in text
    assert document(world.beta)["slack_installations"] == []
    assert {
        "slack_access_settings",
        "slack_allowed_users",
        "slack_allowed_channels",
    } <= set(EXPORT_COLLECTIONS)


def test_platform_read_only_keeps_events_acked_and_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.install()
    controls = world.runtime.platform_controls
    snapshot = controls.snapshot()
    monkeypatch.setattr(
        controls,
        "snapshot",
        lambda: type(snapshot)(**{**vars(snapshot), "read_only": True}),
    )
    assert world.deliver(mention()).status_code == 200
    assert [r.state for r in world.runtime.slack_store.list_events()] == ["pending"]


def test_the_worker_registers_the_lifecycle_tick_and_it_expires_the_inbox(
    world: World,
) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from apps.worker.ticks import build_tick_registry
    from modules.slack.retention import run_tick

    ticks = {
        t.name: t
        for t in build_tick_registry(schedule_interval=1, lifecycle_interval=1).ticks
    }
    assert ticks["slack_lifecycle"].interval_seconds == 600
    world.install()
    world.deliver(mention())
    store = world.runtime.slack_store
    (fresh,) = store.list_events()
    stale = replace(
        fresh,
        event_id="Ev0STALE01",
        message_ts="1.1",
        state="done",
        received_at=fresh.received_at - timedelta(days=8),
    )
    store.record_event(stale)
    assert run_tick(world.runtime.slack_lifecycle) == 1
    assert [e.event_id for e in store.list_events()] == [fresh.event_id]
    assert run_tick(world.runtime.slack_lifecycle) == 0
