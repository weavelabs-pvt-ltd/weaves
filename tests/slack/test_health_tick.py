"""The ``slack_health`` tick and the discovery of usable tools."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from application.ports.slack import SlackInstallation
from apps.worker.ticks import (
    SLACK_HEALTH_SECONDS,
    _refresh_slack_health,
    build_tick_registry,
)
from modules.slack.access.models import ToolHealth
from modules.slack.pipeline.discovery import ToolDiscovery
from modules.slack.pipeline.health import HealthSnapshots, SlackHealthRefresher
from tests.support.slack_pipeline_world import JIRA, ORG, SLACK, World


def _refresher(world: World, **overrides) -> SlackHealthRefresher:
    runtime = world.runtime
    snapshots = HealthSnapshots(
        runtime.slack_connector_health,
        runtime.integration_admin_service.probe_plugin_installation,
    )
    args = {
        "store": runtime.slack_store,
        "installations": runtime.installations,
        "snapshots": snapshots,
        "enabled": lambda: True,
    }
    args.update(overrides)
    return SlackHealthRefresher(**args)


def _status(world: World, installation) -> str:
    return world.runtime.slack_connector_health.get(
        installation.plugin_installation_id
    ).status


def test_the_tick_is_registered_every_ten_minutes_after_the_phase_five_ticks():
    registry = build_tick_registry(schedule_interval=5, lifecycle_interval=30)
    names = [t.name for t in registry.ticks]
    assert (
        names[-1] == "slack_health" and names.index("metrics_gauges") < len(names) - 1
    )
    tick = registry.ticks[-1]
    assert (tick.interval_seconds, tick.run_at_start) == (600, True)
    assert SLACK_HEALTH_SECONDS == 600


def test_the_tick_records_each_connector_without_exposing_a_token(pworld: World):
    assert pworld.jira is not None and pworld.slack is not None
    pworld.connectors.fail_probe.add("jira")

    written = _refresher(pworld).refresh_all()

    assert written == 2
    assert _status(pworld, pworld.slack) == "healthy"
    jira = pworld.runtime.slack_connector_health.get(pworld.jira.plugin_installation_id)
    assert jira.status == "error" and jira.reason_code == "plugin.upstream_error"
    # A status and a reason code only: no credential, no connector data.
    dumped = json.dumps(jira.model_dump(mode="json"))
    assert "fixture" not in dumped and "token" not in dumped.replace("token_", "")
    assert pworld.connectors.total("data") == 0


def test_a_disabled_connector_is_marked_without_a_request(pworld: World):
    assert pworld.jira is not None
    pworld.disable(pworld.jira)

    _refresher(pworld).refresh_all()

    assert _status(pworld, pworld.jira) == "disabled"
    assert (
        pworld.connectors.count("jira") == 0 and pworld.connectors.count("slack") == 1
    )


def test_an_expired_token_is_recorded_as_such(pworld: World, monkeypatch):
    from shared.errors import PluginGatewayError

    def expired(installation):
        raise PluginGatewayError("plugin.oauth_token_error", "expired")

    monkeypatch.setattr(pworld.runtime.jira_plugin, "healthcheck", expired)
    _refresher(pworld).refresh_all()
    assert pworld.jira is not None
    assert _status(pworld, pworld.jira) == "token_expired"


def test_the_tick_does_nothing_while_slack_is_off(pworld: World):
    assert _refresher(pworld, enabled=lambda: False).refresh_all() == 0
    assert pworld.connectors.total() == 0
    assert pworld.runtime.slack_connector_health.list() == ()


def test_the_tick_stops_when_its_time_budget_is_spent(pworld: World):
    assert _refresher(pworld, budget_seconds=0).refresh_all() == 0
    assert pworld.connectors.total() == 0


def test_the_oldest_snapshots_are_refreshed_first(pworld: World):
    assert pworld.jira is not None and pworld.slack is not None
    pworld.snapshot(pworld.slack, "healthy", age_minutes=1)
    pworld.snapshot(pworld.jira, "healthy", age_minutes=30)
    calls: list[str] = []
    runtime = pworld.runtime

    def probe(installation_id: str) -> tuple[str, str]:
        calls.append(installation_id)
        return "healthy", ""

    snapshots = HealthSnapshots(runtime.slack_connector_health, probe)
    refresher = _refresher(pworld, snapshots=snapshots)
    refresher.refresh_all()
    assert calls == [
        pworld.jira.plugin_installation_id,
        pworld.slack.plugin_installation_id,
    ]


def test_snapshots_belong_to_their_organization_and_other_orgs_do_not_break_it(
    pworld: World,
):
    now = datetime.now(UTC)
    pworld.runtime.slack_store.claim(
        SlackInstallation(
            installation_id="slack-inst-2",
            team_id="T0OTHER02",
            org_id="other-org",
            workspace_id="other-workspace",
            bot_user_id="U0BOT2",
            installed_by_principal_id="someone",
            installed_at=now,
            updated_at=now,
        )
    )
    assert _refresher(pworld).refresh_all() == 2  # the other org has no connectors
    rows = pworld.runtime.slack_connector_health.list()
    assert {row.org_id for row in rows} == {ORG}


def test_a_failing_tick_is_logged_and_never_raised(caplog: pytest.LogCaptureFixture):
    def boom() -> int:
        raise RuntimeError("database down")

    services = SimpleNamespace(refresh_slack_health=boom)
    with caplog.at_level(logging.ERROR):
        _refresh_slack_health(services)  # type: ignore[arg-type]
    assert "slack health tick failed" in caplog.text
    _refresh_slack_health(SimpleNamespace(refresh_slack_health=None))  # type: ignore[arg-type]


def _discovery(world: World) -> ToolDiscovery:
    runtime = world.runtime
    return ToolDiscovery(
        installations=runtime.installations,
        plugins=runtime.plugins,
        snapshots=HealthSnapshots(
            runtime.slack_connector_health,
            runtime.integration_admin_service.probe_plugin_installation,
        ),
    )


def test_discovery_reads_only_enabled_read_tools_and_never_calls_a_connector(
    pworld: World,
):
    assert pworld.jira is not None and pworld.slack is not None
    pworld.snapshot(pworld.jira, "healthy")
    pworld.snapshot(pworld.slack, "rate_limited")
    tools = {
        t.tool_id: t
        for t in _discovery(pworld).discover(ORG, "local-workspace", datetime.now(UTC))
    }
    assert set(tools) == {JIRA, SLACK}  # the Jira comment (write) is never a tool
    assert tools[JIRA].health is ToolHealth.HEALTHY
    assert tools[SLACK].health is ToolHealth.UNAVAILABLE  # rate limited: final red
    assert tools[JIRA].presentable and not tools[SLACK].presentable
    assert pworld.connectors.total() == 0


def test_a_missing_old_or_errored_snapshot_means_probe_on_demand(pworld: World):
    assert pworld.jira is not None
    now = datetime.now(UTC)
    discovery = _discovery(pworld)
    found = {t.tool_id: t for t in discovery.discover(ORG, "local-workspace", now)}
    assert found[JIRA].health is None and found[SLACK].health is None  # no snapshot
    pworld.snapshot(pworld.jira, "healthy", age_minutes=16)
    pworld.snapshot(pworld.slack, "error")
    found = {t.tool_id: t for t in discovery.discover(ORG, "local-workspace", now)}
    assert found[JIRA].health is None and not found[JIRA].was_error
    assert found[SLACK].health is None and found[SLACK].was_error
    pworld.snapshot(pworld.jira, "healthy", age_minutes=14)
    found = {t.tool_id: t for t in discovery.discover(ORG, "local-workspace", now)}
    assert found[JIRA].health is ToolHealth.HEALTHY


def test_a_capability_enabled_on_two_installations_is_red_because_it_cannot_run(
    pworld: World,
):
    assert pworld.slack is not None
    pworld.runtime.installations.put(
        pworld.slack.model_copy(
            update={"plugin_installation_id": "dup-slack", "display_name": "Dup"}
        )
    )
    pworld.snapshot(pworld.slack, "healthy")
    tools = [
        t
        for t in _discovery(pworld).discover(ORG, "local-workspace", datetime.now(UTC))
        if t.tool_id == SLACK
    ]
    assert len(tools) == 2
    assert all(t.health is ToolHealth.UNAVAILABLE and not t.presentable for t in tools)
