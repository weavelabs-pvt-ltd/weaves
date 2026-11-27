"""Slack access on PostgreSQL: migration 33, RLS, purge, export and races."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.slack_access_schema import SLACK_ACCESS_MIGRATION
from infrastructure.database.store import SCHEMA_HEAD_VERSION, ProductPostgresStore
from shared.request_context import require_tenant_context
from tests.support.lifecycle_http import EXPORT, REQUEST, drain
from tests.support.operator_scripts import organization_lifecycle, run_script
from tests.support.organization_administration import headers
from tests.support.slack_access_world import (
    ASKER,
    BASE,
    CHANNEL,
    OTHER,
    build_world,
    settings_body,
)


def open_runtime(url, role):
    pool = open_postgres_pool(url, min_size=1, max_size=6)
    store = ProductPostgresStore(pool=pool, app_role=role)
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=store, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    owner = ProductPostgresStore(isolated_postgres_url)
    first, second = (
        open_runtime(isolated_postgres_url, "weaves_app") for _ in range(2)
    )
    operator = open_runtime(isolated_postgres_url, "")
    require_tenant_context(True)
    world = build_world(first)
    world.update(url=isolated_postgres_url, operator=operator, worker=second)
    try:
        with ExitStack() as stack:
            world["clients"] = [
                stack.enter_context(
                    TestClient(create_app(r), raise_server_exceptions=False)
                )
                for r in (first, second)
            ]
            world["client"] = world["clients"][0]
            yield world
    finally:
        require_tenant_context(False)
        for runtime in (first, second, operator):
            runtime.close()
        owner.close()


def sql(world, query, *params):
    with psycopg.connect(world["url"], autocommit=True) as connection:
        return connection.execute(query, params).fetchall()


def slack_counts(world, org):
    return dict(
        sql(
            world,
            "SELECT collection, count(*) FROM product_records "
            "WHERE org_id = %s AND collection LIKE 'slack_%%' GROUP BY collection",
            org,
        )
    )


def seed(world, tenant="a"):
    client, token = world["client"], ("owner_token", "a")
    if tenant == "b":
        token = ("b_token", "b")
    h = headers(world, *token)
    assert (
        client.put(
            BASE, headers=h, json=settings_body(0, ask_enabled=False)
        ).status_code
        == 200
    )
    assert (
        client.post(
            BASE + "/people", headers=h, json={"slack_user_id": ASKER}
        ).status_code
        == 200
    )
    assert (
        client.post(
            BASE + "/channels", headers=h, json={"channel_id": CHANNEL}
        ).status_code
        == 200
    )
    org = world["b" if tenant == "b" else "a"][0].id
    with psycopg.connect(world["url"], autocommit=True) as connection:
        insert(
            connection,
            "slack_feedback",
            f"{org}:turn-1:hmac:1",
            org,
            {
                "schema_version": 1,
                "id": f"{org}:turn-1:hmac:1",
                "org_id": org,
                "answer_ref": "C0CHAN001:1700000001.000100",
                "turn_id": "turn-1",
                "task_id": "task-1",
                "asker_ref": "hmac:1",
                "asker_kind": "hashed",
                "value": 1,
                "source": "slack",
                "at": "2026-11-27T12:00:00+00:00",
                "created_at": "2026-11-27T12:00:00+00:00",
                "updated_at": "2026-11-27T12:00:00+00:00",
            },
        )
    return h


def test_migration_33_is_the_head_and_creates_unique_indexes(world):
    assert SLACK_ACCESS_MIGRATION[0] == 33 <= SCHEMA_HEAD_VERSION
    assert sql(world, "SELECT 1 FROM product_schema_migrations WHERE version = 33")
    names = {
        r[0]
        for r in sql(
            world,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'product_records'",
        )
    }
    assert {
        "slack_allowed_users_org_user_uq",
        "slack_allowed_channels_org_channel_uq",
        "slack_threads_key_uq",
        "slack_feedback_turn_asker_uq",
    } <= names


def insert(connection, collection, record_id, org, payload):
    connection.execute(
        "INSERT INTO product_records (collection, record_id, org_id, payload)"
        " VALUES (%s, %s, %s, %s::jsonb)",
        (collection, record_id, org, json.dumps(payload)),
    )


@pytest.mark.parametrize(
    ("collection", "first", "same_key", "other_key"),
    [
        (
            "slack_allowed_users",
            {"slack_user_id": "U1"},
            {"slack_user_id": "U1"},
            {"slack_user_id": "U2"},
        ),
        (
            "slack_allowed_channels",
            {"channel_id": "C1"},
            {"channel_id": "C1"},
            {"channel_id": "C2"},
        ),
        (
            "slack_threads",
            {"team_id": "T", "channel_id": "C", "thread_ts": "1.1", "kind": "ask"},
            {"team_id": "T", "channel_id": "C", "thread_ts": "1.1", "kind": "ask"},
            {"team_id": "T", "channel_id": "C", "thread_ts": "1.1", "kind": "task"},
        ),
        (
            "slack_feedback",
            {"turn_id": "t1", "asker_ref": "a1"},
            {"turn_id": "t1", "asker_ref": "a1"},
            {"turn_id": "t1", "asker_ref": "a2"},
        ),
    ],
)
def test_unique_per_organization_and_independent_across_organizations(
    world, collection, first, same_key, other_key
):
    with psycopg.connect(world["url"], autocommit=True) as connection:
        insert(connection, collection, "r1", "org-1", first)
        with pytest.raises(psycopg.errors.UniqueViolation):
            insert(connection, collection, "r2", "org-1", same_key)
        insert(connection, collection, "r3", "org-2", same_key)  # another tenant
        insert(connection, collection, "r4", "org-1", other_key)


def test_role_backfill_adds_slack_manage_to_admin_and_owner_roles_only_once(world):
    rows = [
        (
            "role-admin",
            {
                "system_role_key": "admin",
                "name": "Admin",
                "permissions": ["audit.read"],
            },
        ),
        (
            "role-owner",
            {
                "system_role_key": None,
                "name": "Organization owner",
                "permissions": ["audit.read"],
            },
        ),
        (
            "role-builder",
            {
                "system_role_key": "builder",
                "name": "Builder",
                "permissions": ["agents.view"],
            },
        ),
        ("role-custom", {"system_role_key": None, "name": "Custom", "permissions": []}),
        (
            "role-done",
            {
                "system_role_key": "admin",
                "name": "Admin",
                "permissions": ["slack.manage", "audit.read"],
            },
        ),
    ]
    with psycopg.connect(world["url"], autocommit=True) as connection:
        for record_id, payload in rows:
            insert(
                connection,
                "roles",
                record_id,
                "org-y" if record_id == "role-done" else "org-x",
                payload,
            )
        for _ in range(3):  # idempotent: re-running changes nothing further
            for statement in SLACK_ACCESS_MIGRATION[1]:
                connection.execute(statement)
        got = dict(
            connection.execute(
                "SELECT record_id, payload->'permissions' FROM product_records "
                "WHERE collection = 'roles' AND org_id IN ('org-x', 'org-y')"
            ).fetchall()
        )
    assert got["role-admin"] == ["audit.read", "slack.manage"]
    assert got["role-owner"] == ["audit.read", "slack.manage"]
    assert got["role-builder"] == ["agents.view"]
    assert got["role-custom"] == []
    assert got["role-done"] == ["slack.manage", "audit.read"]


def test_role_seeding_path_gives_admin_and_nobody_else_the_permission(world):
    stored = sql(
        world,
        "SELECT payload->>'system_role_key', payload->'permissions' "
        "FROM product_records WHERE collection = 'roles' AND org_id = %s",
        world["a"][0].id,
    )
    by_key = {key: perms for key, perms in stored}
    assert "slack.manage" in by_key["admin"]
    assert (
        "slack.manage" not in by_key["builder"] and "slack.manage" not in by_key["user"]
    )


def test_restricted_role_sees_only_its_own_organizations_slack_rows(world):
    seed(world)
    seed(world, "b")
    a, b = world["a"][0].id, world["b"][0].id
    with psycopg.connect(world["url"]) as connection:
        connection.execute("SET ROLE weaves_app")
        connection.execute("SELECT set_config('app.org_id', %s, false)", (a,))
        seen = {
            r[0]
            for r in connection.execute(
                "SELECT DISTINCT org_id FROM product_records "
                "WHERE collection LIKE 'slack_%'"
            )
        }
        assert seen == {a}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            insert(
                connection, "slack_allowed_users", "forged", b, {"slack_user_id": "U9"}
            )
    assert slack_counts(world, a) == slack_counts(world, b)


def test_only_slack_managers_and_each_tenant_stays_separate_on_postgres(world):
    client = world["client"]
    h = seed(world)
    member = headers(world, "member_token")
    assert client.get(BASE, headers=member).status_code == 403
    assert (
        client.post(
            BASE + "/people", headers=member, json={"slack_user_id": OTHER}
        ).status_code
        == 403
    )
    other = headers(world, "b_token", "b")
    assert client.get(BASE, headers=other).json()["settings_version"] == 0
    assert client.get(BASE + "/people", headers=other).json() == []
    assert client.get(BASE, headers=h).json()["settings_version"] == 3


def race(*operations):
    barrier = Barrier(len(operations))

    def run(operation):
        barrier.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def test_two_api_processes_racing_one_settings_version_have_one_winner(world):
    h = headers(world)
    first, second = world["clients"]
    body_a = settings_body(0, ask_enabled=False)
    body_b = settings_body(0, partial_answers=True)
    results = race(
        lambda: first.put(BASE, headers=h, json=body_a),
        lambda: second.put(BASE, headers=h, json=body_b),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    final = first.get(BASE, headers=h).json()
    assert final["settings_version"] == 1
    assert (final["ask_enabled"], final["partial_answers"]) in {
        (False, False),
        (True, True),
    }


def test_concurrent_allowlist_changes_all_land_with_distinct_versions(world):
    h = headers(world)
    clients = world["clients"]
    ids = [f"U0RACE{i:03d}" for i in range(6)]
    results = race(
        *[
            (
                lambda c=clients[i % 2], uid=uid: c.post(
                    BASE + "/people", headers=h, json={"slack_user_id": uid}
                )
            )
            for i, uid in enumerate(ids)
        ]
    )
    assert [r.status_code for r in results] == [200] * 6
    view = clients[0].get(BASE, headers=h).json()
    assert view["settings_version"] == 6 and view["allowed_people_count"] == 6
    versions = sorted(
        e[0]
        for e in sql(
            world,
            "SELECT (payload->'metadata'->>'settings_version')::int "
            "FROM product_records WHERE collection = 'audit_events' AND org_id = %s "
            "AND payload->>'action' = 'slack.access.user_added'",
            world["a"][0].id,
        )
    )
    assert versions == [1, 2, 3, 4, 5, 6]


def test_export_includes_slack_access_but_no_message_text(world):
    seed(world)
    response = world["client"].post(EXPORT, headers=headers(world))
    assert response.status_code == 200, response.text
    data = json.loads(response.text)["data"]
    assert len(data["slack_access_settings"]) == 1
    assert [r["slack_user_id"] for r in data["slack_allowed_users"]] == [ASKER]
    assert [r["channel_id"] for r in data["slack_allowed_channels"]] == [CHANNEL]
    (rating,) = data["slack_feedback"]
    assert (rating["value"], rating["asker_kind"]) == (1, "hashed")
    assert "text" not in rating and "reason" not in rating


class _Capture:
    def readouterr(self):
        return type("Out", (), {"out": "", "err": ""})()


def test_purge_removes_the_organizations_slack_rows_and_only_those(world):
    seed(world)
    seed(world, "b")
    a, b = world["a"][0].id, world["b"][0].id
    before_b = slack_counts(world, b)
    assert set(slack_counts(world, a)) == {
        "slack_access_settings",
        "slack_allowed_users",
        "slack_allowed_channels",
        "slack_feedback",
    }
    deleted = world["client"].post(
        REQUEST, headers=headers(world), json={"confirm_name": "Org A"}
    )
    assert deleted.status_code == 200, deleted.text
    code = run_script(
        organization_lifecycle,
        _Capture(),
        world["operator"],
        "purge-now",
        "--organization-id",
        a,
        "--operator",
        "ops@example.test",
        "--reason",
        "pg test",
        "--confirm-organization-name",
        "Org A",
    )[0]
    assert code == 0
    drain(world["worker"])
    assert slack_counts(world, a) == {}
    assert slack_counts(world, b) == before_b
    kept = sql(
        world,
        "SELECT DISTINCT payload->>'action' FROM product_records "
        "WHERE collection = 'audit_events' AND org_id = %s",
        a,
    )
    assert all(r[0].startswith("organization.lifecycle.") for r in kept)
