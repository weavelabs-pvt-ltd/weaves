"""Slack pipeline pieces on PostgreSQL under the restricted application role."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet

from application.ports.slack import SlackInstallation
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import TaskTurn, TurnOrigin, TurnVerdict
from contracts.v1.slack_access import SlackConnectorHealth
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.slack.pipeline.health import HealthSnapshots, SlackHealthRefresher
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
)
from tests.support.organization_administration import prepare, scope
from tests.support.slack_access_world import install_connector


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=6)
    store = ProductPostgresStore(pool=pool, app_role="weaves_app")
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    require_tenant_context(True)
    built = {"runtime": runtime, **prepare(runtime)}
    install_connector(runtime, built["a"], "inst-a-jira")
    install_connector(runtime, built["b"], "inst-b-jira")
    try:
        yield built
    finally:
        require_tenant_context(False)
        runtime.close()


def _snapshot(
    org_id: str, installation_id: str, status="healthy"
) -> SlackConnectorHealth:
    now = datetime.now(UTC)
    return SlackConnectorHealth(
        id=installation_id,
        org_id=org_id,
        installation_id=installation_id,
        status=status,
        checked_at=now,
        created_at=now,
        updated_at=now,
    )


def test_health_snapshots_are_tenant_isolated_by_row_security(world):
    runtime = world["runtime"]
    a, b = world["a"], world["b"]
    with scope(a):
        runtime.slack_connector_health.put(_snapshot(a[0].id, "inst-a-jira"))
    with scope(b):
        assert runtime.slack_connector_health.list() == ()
        with pytest.raises(KeyError):
            runtime.slack_connector_health.get_scoped("inst-a-jira", a[0].id)
        runtime.slack_connector_health.put(_snapshot(b[0].id, "inst-b-jira", "error"))
    with scope(a):
        assert [r.id for r in runtime.slack_connector_health.list()] == ["inst-a-jira"]
    token = set_request_identity("nobody", "unrelated-org", "ws")
    try:
        assert runtime.slack_connector_health.list() == ()
    finally:
        reset_request_principal(token)


def test_the_tick_refreshes_each_organization_inside_its_own_tenant_scope(world):
    runtime = world["runtime"]
    now = datetime.now(UTC)
    for key in ("a", "b"):
        org, workspace = world[key][0], world[key][1]
        runtime.slack_store.claim(
            SlackInstallation(
                installation_id=f"slack-{key}",
                team_id=f"T0TEAM00{key.upper()}",
                org_id=org.id,
                workspace_id=workspace.id,
                bot_user_id="U0BOT",
                installed_by_principal_id=world[key][3].id,
                installed_at=now,
                updated_at=now,
            )
        )
    probed: list[tuple[str, str]] = []

    def probe(installation_id: str) -> tuple[str, str]:
        from shared.request_context import effective_org_id

        probed.append((effective_org_id(), installation_id))
        return (
            ("healthy", "") if installation_id.startswith("inst-a") else ("error", "x")
        )

    refresher = SlackHealthRefresher(
        store=runtime.slack_store,
        installations=runtime.installations,
        snapshots=HealthSnapshots(runtime.slack_connector_health, probe),
        enabled=lambda: True,
    )
    assert refresher.refresh_all() == 2  # a bare worker tick: no tenant identity
    # Each connector was probed as its own organization, never the other's.
    assert sorted(probed) == sorted(
        [(world["a"][0].id, "inst-a-jira"), (world["b"][0].id, "inst-b-jira")]
    )
    with scope(world["a"]):
        (row,) = runtime.slack_connector_health.list()
        assert (row.id, row.status) == ("inst-a-jira", "healthy")
    with scope(world["b"]):
        (row,) = runtime.slack_connector_health.list()
        assert (row.id, row.status, row.reason_code) == ("inst-b-jira", "error", "x")


def test_a_turn_origin_with_its_verdict_survives_storage(world):
    runtime = world["runtime"]
    a = world["a"]
    origin = TurnOrigin(
        kind="slack_qa",
        team_id="T0TEAM00A",
        channel_id="C0CHAN001",
        thread_ts="1700000000.000100",
        slack_user_id="U0ASKER01",
        event_id="evt-1",
        capability_ids=("jira.issues.search",),
        verdict=TurnVerdict(outcome="allowed", planner_status="valid"),
    )
    with scope(a):
        agent = runtime.agent_service.create_agent(name="Q&A", instructions="Answer.")
        task = runtime.task_service.create_task(
            agent_id=agent.id, title="Slack", initial_message="hello"
        )
        turn = runtime.task_turns.get(task.active_turn_id)
        runtime.task_turns.put(
            TaskTurn.model_validate(
                turn.model_copy(update={"origin": origin}).model_dump()
            )
        )
        stored = runtime.task_turns.get(turn.turn_id)
    assert stored.origin == origin
