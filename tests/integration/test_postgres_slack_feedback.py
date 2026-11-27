"""Reaction feedback on PostgreSQL under the restricted application role.

Real tables with row-level security: the tick writes one row per turn and asker,
the unique key refuses a second one, tenants and system purposes cannot read each
other's ratings, the ``feedback`` purpose reaches only this collection, retention
deletes in bounded batches and the analytics read path returns counts and ids
without any asker.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from contracts.v1.slack_feedback import SlackFeedback
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.slack.feedback.retention import FeedbackRetention
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
    system_scope,
)
from tests.support import slack_pipeline_world
from tests.support.slack_ask_world import AskWorld, build_ask_world
from tests.support.slack_feedback_world import ORG, USER, answered, react

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def pg_world(isolated_postgres_url, monkeypatch):
    """The Ask world over PostgreSQL under the restricted application role."""
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "true")
    monkeypatch.setenv("WEAVES_SLACK_SIGNING_SECRET", "fixture-signing-secret")

    def restricted_runtime():
        pool = open_postgres_pool(isolated_postgres_url, min_size=1, max_size=12)
        store = ProductPostgresStore(pool=pool, app_role="weaves_app")
        return compose_platform_runtime(http_client=httpx.Client(), store=store)

    monkeypatch.setattr(
        slack_pipeline_world, "LocalPlatformRuntime", restricted_runtime
    )
    world = build_ask_world()
    try:
        yield world
    finally:
        world.world.close()


def rating(
    n: int, at: datetime = NOW, org: str = ORG, **fields: object
) -> SlackFeedback:
    values: dict[str, object] = {
        "id": f"{org}:turn-{n}:hmac:{n}",
        "org_id": org,
        "answer_ref": f"C0CHAN001:{n}.000100",
        "turn_id": f"turn-{n}",
        "task_id": f"task-{n}",
        "request_id": f"slack-Ev{n}",
        "asker_ref": f"hmac:{n}",
        "asker_kind": "hashed",
        "value": 1,
        "at": at,
        "created_at": at,
        "updated_at": at,
        **fields,
    }
    return SlackFeedback(**values)  # type: ignore[arg-type]


def stored_rows(pg_world: AskWorld, query: str, *params: object) -> list[tuple]:
    store = pg_world.runtime.slack_feedback_admin._store
    assert isinstance(store, ProductPostgresStore)
    with store.pool.connection() as connection:
        return connection.execute(query, params).fetchall()


def test_a_reaction_becomes_one_row_and_the_latest_wins_under_row_security(
    pg_world: AskWorld,
) -> None:
    thread = answered(pg_world)
    react(pg_world, thread.answer_ts, reaction="+1", received_at=NOW)
    react(
        pg_world,
        thread.answer_ts,
        reaction="thumbsdown::skin-tone-2",
        received_at=NOW + timedelta(minutes=1),
    )

    assert pg_world.runtime.slack_feedback_tick.run() == 2

    (row,) = pg_world.runtime.slack_feedback.list()
    assert (row.value, row.at) == (-1, NOW + timedelta(minutes=1))
    assert row.asker_kind == "hashed" and USER not in row.model_dump_json()
    assert row.agent_id and row.model_id and row.agent_run_id


def test_the_unique_key_refuses_a_second_row_for_one_turn_and_asker(
    pg_world: AskWorld,
) -> None:
    repository = pg_world.runtime.slack_feedback
    repository.create(rating(1))
    with pytest.raises(ValueError):
        repository.create(rating(1).model_copy(update={"id": "another-id"}))


def test_ratings_are_invisible_to_other_tenants_and_other_system_purposes(
    pg_world: AskWorld,
) -> None:
    runtime = pg_world.runtime
    runtime.slack_feedback.put(rating(1))
    token = set_request_identity("nobody", "unrelated-org", "ws")
    try:
        assert runtime.slack_feedback.list() == ()
        assert runtime.slack_feedback.list_by_field("turn_id", "turn-1") == ()
        with pytest.raises(KeyError):
            runtime.slack_feedback.get_scoped(rating(1).id, ORG)
    finally:
        reset_request_principal(token)
    store = runtime.slack_feedback_admin._store
    require_tenant_context(True)  # production shape: no ambient local tenant
    try:
        for purpose in ("slack", "usage", "queue", "auth", "waitlist", "lifecycle"):
            with system_scope(purpose), store.connection() as connection:
                found = connection.execute(
                    "SELECT count(*) FROM product_records "
                    "WHERE collection = 'slack_feedback'"
                ).fetchone()
            assert found[0] == 0, purpose
    finally:
        require_tenant_context(False)


def test_the_feedback_purpose_reaches_only_this_collection_across_organizations(
    pg_world: AskWorld,
) -> None:
    runtime = pg_world.runtime
    runtime.slack_feedback.put(rating(1))
    other = set_request_identity("local-developer", "org-b", "ws-b")
    try:
        runtime.slack_feedback.put(rating(2, org="org-b"))
    finally:
        reset_request_principal(other)
    store = runtime.slack_feedback_admin._store
    require_tenant_context(True)
    try:
        with system_scope("feedback"), store.connection() as connection:
            collections = connection.execute(
                "SELECT DISTINCT collection FROM product_records "
                "WHERE org_id IS NOT NULL"
            ).fetchall()
            orgs = connection.execute(
                "SELECT DISTINCT org_id FROM product_records "
                "WHERE collection = 'slack_feedback'"
            ).fetchall()
    finally:
        require_tenant_context(False)
    assert collections == [("slack_feedback",)]
    assert {r[0] for r in orgs} == {ORG, "org-b"}


def test_counts_and_lookup_run_across_organizations_without_any_asker(
    pg_world: AskWorld,
) -> None:
    runtime = pg_world.runtime
    runtime.slack_feedback.put(
        rating(1, agent_id="a1", model_id="m1", connector_set="jira")
    )
    runtime.slack_feedback.put(
        rating(2, value=-1, agent_id="a1", model_id="m1", connector_set="jira")
    )
    token = set_request_identity("local-developer", "org-b", "ws-b")
    try:
        runtime.slack_feedback.put(rating(3, org="org-b", at=NOW + timedelta(days=40)))
    finally:
        reset_request_principal(token)
    admin = runtime.slack_feedback_admin

    day = admin.counts(date(2026, 11, 1), date(2027, 1, 31), by="day")
    assert [(r.period, r.org_id, r.agent_id, r.up, r.down) for r in day] == [
        ("2026-11-27", ORG, "a1", 1, 1),
        ("2027-01-06", "org-b", "", 1, 0),
    ]
    assert [
        r.period for r in admin.counts(date(2026, 11, 1), date(2027, 1, 31), by="month")
    ] == ["2026-11", "2027-01"]
    assert (
        len(
            admin.counts(date(2026, 11, 1), date(2027, 1, 31), by="day", org_id="org-b")
        )
        == 1
    )
    (found,) = admin.by_request("slack-Ev2")
    assert found["turn_id"] == "turn-2" and found["value"] == -1
    assert "asker_ref" not in found and json.dumps(found).count("hmac") == 0


def test_retention_deletes_oldest_first_in_bounded_batches(
    pg_world: AskWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = pg_world.runtime
    for n in range(6):
        runtime.slack_feedback.put(rating(n, at=NOW - timedelta(days=401 + n)))
    runtime.slack_feedback.put(rating(40, at=NOW - timedelta(days=10)))
    from modules.slack.feedback import retention

    monkeypatch.setattr(retention, "BATCH", 2)
    monkeypatch.setattr(retention, "MAX_BATCHES", 2)
    expiry = FeedbackRetention(
        runtime.slack_feedback_admin, days=400, clock=lambda: NOW
    )

    assert expiry.run() == 4
    assert sorted(r.turn_id for r in runtime.slack_feedback.list()) == [
        "turn-0",
        "turn-1",
        "turn-40",
    ]
    assert expiry.run() == 2
    assert [r.turn_id for r in runtime.slack_feedback.list()] == ["turn-40"]
    assert expiry.run() == 0


def test_the_stored_row_has_no_text_no_reason_and_no_raw_slack_id(
    pg_world: AskWorld,
) -> None:
    thread = answered(pg_world, "what does ZEBRA-7731 idempotent mean?")
    react(pg_world, thread.answer_ts)
    pg_world.runtime.slack_feedback_tick.run()

    rows = stored_rows(
        pg_world,
        "SELECT payload::text FROM product_records WHERE collection = 'slack_feedback'",
    )
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert set(payload) == {
        "schema_version", "id", "org_id", "answer_ref", "turn_id", "task_id",
        "agent_run_id", "request_id", "asker_ref", "asker_kind", "value", "source",
        "at", "agent_id", "model_id", "connector_set", "created_at", "updated_at",
    }  # fmt: skip
    for marker in (USER, "ZEBRA", "idempotent"):
        assert marker not in rows[0][0]
