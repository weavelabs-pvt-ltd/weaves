"""Slack tables on PostgreSQL under the restricted role (migration 32)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from threading import Barrier
from typing import Any

import psycopg
import pytest
from cryptography.fernet import Fernet

from application.ports.slack import InboxEvent, SlackInstallation
from infrastructure.database import store as store_module
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.slack_schema import SLACK_MIGRATION
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from infrastructure.persistence.slack_store import PostgresSlackStore
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.slack.support import (
    BOT_TOKEN,
    World,
    lifecycle_event,
    mention,
    running_world,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
OTHER_PURPOSES = (
    "bootstrap",
    "auth",
    "queue",
    "dispatch",
    "recovery",
    "studio",
    "account",
    "lifecycle",
    "catalog",
    "oauth",
    "usage",
    "waitlist",
)


def installation(org: str, team: str = "T0TEAM001", **fields: Any) -> SlackInstallation:
    return SlackInstallation(
        **{
            "installation_id": f"inst-{org}-{team}",
            "team_id": team,
            "org_id": org,
            "workspace_id": "ws",
            "bot_user_id": "U0BOT",
            "installed_by_principal_id": "principal-1",
            "installed_at": NOW,
            "updated_at": NOW,
            "bot_secret_ref": f"secret:{org}",
            "scopes": ("chat:write",),
            **fields,
        }
    )


def event(event_id: str, ts: str = "1.1", team: str = "T0TEAM001") -> InboxEvent:
    return InboxEvent(
        event_id=event_id,
        team_id=team,
        event_type="app_mention",
        received_at=NOW,
        channel_id="C1",
        message_ts=ts,
        user_id="U1",
        text="hello",
    )


@pytest.fixture
def store(isolated_postgres_url):
    pg = ProductPostgresStore(
        pool=open_postgres_pool(isolated_postgres_url, min_size=1, max_size=20),
        app_role="weaves_app",
    )
    yield pg
    pg.close()
    require_tenant_context(False)


@pytest.fixture
def slack(store):
    return PostgresSlackStore(store)


def owner_sql(url: str, query: str, *params: Any) -> list[Any]:
    with psycopg.connect(url, autocommit=True) as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def as_tenant(org: str):
    return set_request_identity("principal", org, "ws")


def test_migration_32_is_the_head_and_idempotent(isolated_postgres_url):
    assert SLACK_MIGRATION[0] == 32 and SCHEMA_HEAD_VERSION >= 32
    ProductPostgresStore(isolated_postgres_url).close()
    ProductPostgresStore(isolated_postgres_url).close()  # second start: no-op
    rows = owner_sql(
        isolated_postgres_url,
        "SELECT relname, relrowsecurity FROM pg_class WHERE relname IN "
        "('slack_installations', 'slack_event_inbox') "
        "AND relnamespace = current_schema()::regnamespace ORDER BY relname",
    )
    assert rows == [("slack_event_inbox", True), ("slack_installations", True)]
    with psycopg.connect(isolated_postgres_url, autocommit=True) as connection:
        for statement in SLACK_MIGRATION[1]:
            connection.execute(statement)  # a manual re-run changes nothing


def test_migration_32_applies_over_a_database_at_version_31(
    isolated_postgres_url, monkeypatch
):
    previous = tuple(m for m in store_module._SCHEMA_MIGRATIONS if m[0] < 32)
    monkeypatch.setattr(store_module, "_SCHEMA_MIGRATIONS", previous)
    monkeypatch.setattr(
        store_module, "KNOWN_SCHEMA_VERSIONS", frozenset(m[0] for m in previous) | {20}
    )
    ProductPostgresStore(isolated_postgres_url).close()
    monkeypatch.undo()
    assert not owner_sql(
        isolated_postgres_url,
        "SELECT 1 FROM pg_tables WHERE tablename = 'slack_installations' "
        "AND schemaname = current_schema()",
    )
    ProductPostgresStore(isolated_postgres_url).close()
    assert owner_sql(
        isolated_postgres_url,
        "SELECT 1 FROM product_schema_migrations WHERE version = 32",
    )


def test_a_tenant_sees_only_its_own_installations(slack):
    assert slack.claim(installation("org-a", "T0A")).outcome == "created"
    assert slack.claim(installation("org-b", "T0B")).outcome == "created"
    token = as_tenant("org-a")
    try:
        assert [i.team_id for i in slack.list_for_org()] == ["T0A"]
        assert slack.get_for_org("inst-org-b-T0B") is None
        with slack._store.connection() as connection:
            assert not connection.execute(
                "SELECT 1 FROM slack_installations WHERE org_id = 'org-b'"
            ).fetchall()
            hijack = connection.execute(
                "UPDATE slack_installations SET status = 'revoked' "
                "WHERE org_id = 'org-b'"
            )
            assert hijack.rowcount == 0
    finally:
        reset_request_principal(token)


def test_only_the_slack_purpose_sees_across_tenants_and_the_inbox(slack, store):
    slack.claim(installation("org-a", "T0A"))
    slack.claim(installation("org-b", "T0B"))
    assert slack.record_event(event("Ev1"))
    tables = ("slack_installations", "slack_event_inbox")

    def count(table: str) -> int:
        with store.connection() as connection:
            return int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )

    with system_scope("slack"):
        assert (count(tables[0]), count(tables[1])) == (2, 1)
    for purpose in (None, *OTHER_PURPOSES):
        with system_scope(purpose) if purpose else nullcontext():
            assert (count(tables[0]), count(tables[1])) == (0, 0), purpose
    token = as_tenant("org-a")
    try:
        assert count(tables[0]) == 1  # its own row only
        assert count(tables[1]) == 0  # the inbox is closed to every tenant
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            store.connection() as connection,
        ):
            connection.execute(
                "INSERT INTO slack_event_inbox (event_id, team_id, event_type, "
                "state, received_at) VALUES ('x', 'T', 'app_mention', 'pending', "
                "now())"
            )
    finally:
        reset_request_principal(token)


def test_a_workspace_belongs_to_exactly_one_organization(slack, store):
    assert slack.claim(installation("org-a")).outcome == "created"
    refused = slack.claim(installation("org-b"))
    assert refused.outcome == "conflict" and refused.installation is None
    assert slack.find_by_team("T0TEAM001").org_id == "org-a"
    # The unique index is the backstop even for a writer that skips the claim.
    with (
        pytest.raises(psycopg.errors.UniqueViolation),
        system_scope("slack"),
        store.connection() as connection,
    ):
        connection.execute(
            "INSERT INTO slack_installations (installation_id, team_id, org_id, "
            "workspace_id, bot_user_id, installed_by_principal_id, status, "
            "installed_at, updated_at) VALUES ('rogue', 'T0TEAM001', 'org-b', "
            "'ws', 'U', 'p', 'active', now(), now())"
        )


def test_concurrent_claims_by_two_organizations_have_one_winner(slack):
    barrier = Barrier(8)

    def claim(index: int) -> str:
        barrier.wait()
        org = f"org-{index % 2}"
        candidate = installation(org, installation_id=f"inst-{index}")
        return slack.claim(candidate).outcome

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = sorted(pool.map(claim, range(8)))
    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") + outcomes.count("updated") == 7
    holder = slack.find_by_team("T0TEAM001")
    assert holder is not None
    others = [o for o in outcomes if o != "created"]
    assert others.count("updated") == 3  # the winner's own org reinstalling


def test_reinstall_updates_in_place_and_a_freed_workspace_can_move(slack):
    slack.claim(installation("org-a"))
    again = slack.claim(
        installation("org-a", installation_id="new-id", bot_secret_ref="secret:new")
    )
    assert again.outcome == "updated"
    assert again.previous_secret_ref == "secret:org-a"
    assert again.installation.installation_id == "inst-org-a-T0TEAM001"
    token = as_tenant("org-a")
    try:
        assert len(slack.list_for_org()) == 1
        assert slack.set_status(
            "inst-org-a-T0TEAM001", "uninstalled", NOW, clear_secret=True
        )
    finally:
        reset_request_principal(token)
    assert slack.find_by_team("T0TEAM001") is None
    assert slack.claim(installation("org-b")).outcome == "created"
    # The first organization's history row stays; it may not retake a held workspace.
    assert slack.claim(installation("org-a")).outcome == "conflict"


def test_event_ids_are_deduplicated_under_concurrency(slack):
    barrier = Barrier(16)

    def record(_: int) -> bool:
        barrier.wait()
        return slack.record_event(event("Ev0SAME"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(record, range(16)))
    assert results.count(True) == 1
    assert [e.event_id for e in slack.list_events()] == ["Ev0SAME"]


def test_one_message_under_two_event_ids_is_one_row(slack):
    assert slack.record_event(event("Ev1", ts="9.9"))
    assert not slack.record_event(event("Ev2", ts="9.9"))
    assert slack.record_event(event("Ev3", ts="9.8"))
    assert [e.event_id for e in slack.list_events()] == ["Ev1", "Ev3"]


def test_purge_removes_only_processed_rows(slack):
    slack.record_event(event("Ev1", ts="1.1"))
    done = InboxEvent(
        event_id="Ev2",
        team_id="T0TEAM001",
        event_type="app_mention",
        received_at=NOW,
        state="done",
        channel_id="C",
        message_ts="2.2",
    )
    slack.record_event(done)
    assert slack.purge_events(datetime(2027, 1, 1, tzinfo=UTC)) == 1
    assert [e.event_id for e in slack.list_events()] == ["Ev1"]


@pytest.fixture
def pg_world(isolated_postgres_url, monkeypatch, tmp_path):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_TOMBSTONE_DIR", str(tmp_path / "tombstones"))
    with running_world(monkeypatch, database_url=isolated_postgres_url) as running:
        yield running


def test_install_events_and_disconnect_work_end_to_end_on_postgres(
    pg_world: World, isolated_postgres_url, tmp_path
):
    world = pg_world
    assert world.install().status_code == 303
    (view,) = world.status()["installations"]
    assert view["status"] == "active" and view["installed_by"] == "Owner"
    world.api.calls.clear()
    world.api.tokens.clear()
    assert world.deliver(mention()).status_code == 200
    assert world.deliver(mention(), extra={"x-slack-retry-num": "1"}).status_code == 200
    rows = world.runtime.slack_store.list_events()
    assert [r.state for r in rows] == ["pending"]
    assert world.api.methods() == ["reactions.add"] and world.api.tokens == [BOT_TOKEN]
    # The bot token exists nowhere in the Slack tables, in clear text.
    dump = json.dumps(
        owner_sql(isolated_postgres_url, "SELECT * FROM slack_installations")
        + owner_sql(isolated_postgres_url, "SELECT * FROM slack_event_inbox"),
        default=str,
    )
    assert BOT_TOKEN not in dump
    ciphertext = owner_sql(
        isolated_postgres_url, "SELECT ciphertext FROM product_secrets"
    )
    assert ciphertext and all(BOT_TOKEN.encode() not in bytes(c[0]) for c in ciphertext)
    # A second organization cannot take the workspace.
    assert (
        "workspace_already_connected"
        in world.install(world.beta, code="c2").headers["location"]
    )
    # app_uninstalled ends it: the secret goes and a tombstone is written first.
    assert world.deliver(lifecycle_event("app_uninstalled")).status_code == 200
    assert world.status()["installations"][0]["status"] == "uninstalled"
    assert not owner_sql(
        isolated_postgres_url,
        "SELECT 1 FROM product_secrets WHERE purpose = 'slack_bot_token'",
    )
    tombstones = (tmp_path / "tombstones" / "tombstones.jsonl").read_text()
    assert '"secret_deleted"' in tombstones and BOT_TOKEN not in tombstones
    assert world.deliver(mention(event_id="Ev0NEW", ts="5.5")).status_code == 200
    assert len(world.runtime.slack_store.list_events()) == 1
