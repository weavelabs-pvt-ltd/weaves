"""S7 on PostgreSQL: purge, quarantine and reinstall, as the restricted role."""

from __future__ import annotations

import json
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet

from apps.runtime_wiring import compose_platform_runtime
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.slack_store import PostgresSlackStore
from infrastructure.secrets.store import InMemorySecretStore
from modules.platform_controls.quarantine import QuarantineService
from modules.recovery.tombstones import FileTombstoneLog, TombstoneWriteError
from modules.slack import retention
from modules.slack.lifecycle import SlackLifecycle
from shared.request_context import (
    require_tenant_context,
    reset_request_principal,
    set_request_identity,
)
from tests.integration.test_postgres_slack import event, installation
from tests.slack.support import (
    BOT_TOKEN,
    World,
    access_payload,
    lifecycle_event,
    mention,
    running_world,
)

BETA_TOKEN = "xoxb-beta-token-must-never-leak"
TEAM_A, TEAM_B = "T0ACME001", "T0BETA001"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
OPERATOR = ("ops@weaves.test", "test")


@dataclass
class Stack:
    world: World
    operator: Any
    log: FileTombstoneLog
    url: str


@pytest.fixture
def stack(isolated_postgres_url, monkeypatch, tmp_path) -> Iterator[Stack]:
    monkeypatch.setenv("WEAVES_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("WEAVES_TOMBSTONE_DIR", str(tmp_path / "tombstones"))
    with running_world(monkeypatch, database_url=isolated_postgres_url) as world:
        world.install()
        world.api.access = access_payload(team=TEAM_B, token=BETA_TOKEN)
        world.install(world.beta, code="b1")
        world.api.calls.clear()
        world.api.tokens.clear()
        operator = compose_platform_runtime(
            http_client=httpx.Client(transport=world.api.transport()),
            store=ProductPostgresStore(
                pool=open_postgres_pool(isolated_postgres_url, max_size=4),
                app_role="",
            ),
            seed_local_defaults=False,
        )
        require_tenant_context(True)
        try:
            yield Stack(
                world,
                operator,
                FileTombstoneLog(tmp_path / "tombstones"),
                isolated_postgres_url,
            )
        finally:
            operator.close()


def sql(url: str, query: str, *params: Any) -> list[Any]:
    with psycopg.connect(url, autocommit=True) as connection:
        cursor = connection.execute(query, params)
        return cursor.fetchall() if cursor.description else []


def counts(stack: Stack, org_id: str, team: str) -> tuple[int, int, int]:
    """Installation rows, inbox rows and slack secrets of one organization."""
    return (
        sql(
            stack.url,
            "SELECT count(*) FROM slack_installations WHERE org_id=%s",
            org_id,
        )[0][0],
        sql(stack.url, "SELECT count(*) FROM slack_event_inbox WHERE team_id=%s", team)[
            0
        ][0],
        sql(
            stack.url,
            "SELECT count(*) FROM product_secrets WHERE purpose='slack_bot_token' "
            "AND org_id=%s",
            org_id,
        )[0][0],
    )


def request_purge(stack: Stack, org: Any, name: str) -> None:
    service = stack.world.runtime.organization_lifecycle_service
    service.operator_request_deletion(org.org_id, *OPERATOR)
    service.purge_now(org.org_id, *OPERATOR, name)


def drain(stack: Stack, hook=None, ticks: int = 12) -> None:
    purge = stack.world.runtime.organization_purge_service
    purge.hook = hook
    for _ in range(ticks):
        purge.tick()
    purge.hook = None


def deleted(stack: Stack, org: Any) -> bool:
    token = set_request_identity("p", org.org_id, org.workspace_id)
    try:
        state = stack.world.runtime.organization_lifecycle_service.state_of(org.org_id)
        return state.value == "deleted"
    finally:
        reset_request_principal(token)


def test_purge_revokes_then_tombstones_then_deletes_and_leaves_the_other_tenant(
    stack: Stack,
) -> None:
    world, a, b = stack.world, stack.world.alpha, stack.world.beta
    world.deliver(mention())
    world.deliver(mention(team=TEAM_B, event_id="Ev0BBB001"))
    assert counts(stack, a.org_id, TEAM_A) == (1, 1, 1)
    request_purge(stack, a, "Alpha")
    drain(stack)
    assert deleted(stack, a)
    assert counts(stack, a.org_id, TEAM_A) == (0, 0, 0)
    assert counts(stack, b.org_id, TEAM_B) == (1, 1, 1)  # tenant B untouched
    # The bot token was revoked at Slack (once, and only A's token).
    assert world.api.methods().count("auth.revoke") == 1
    assert world.api.tokens[world.api.methods().index("auth.revoke")] == BOT_TOKEN
    kinds = [t.kind for t in stack.log.read() if t.org_id == a.org_id]
    assert kinds[0] == "organization_purge_started"
    assert kinds.index("slack_installation_ended") < kinds.index("secret_deleted")
    assert kinds[-1] == "organization_deleted"
    assert "slack" in stack.log.read()[-1].erased
    text = stack.log.path.read_text()
    assert BOT_TOKEN not in text and BETA_TOKEN not in text


@pytest.mark.parametrize(
    "step",
    ["slack", "slack_tombstone", "slack_revoke", "slack_row", "slack_inbox"],
)
def test_a_crash_at_each_slack_step_resumes_and_converges(
    stack: Stack, step: str
) -> None:
    world, a, b = stack.world, stack.world.alpha, stack.world.beta
    world.deliver(mention())
    request_purge(stack, a, "Alpha")
    purge = world.runtime.organization_purge_service
    crashed: list[str] = []

    def hook(name: str) -> None:
        if name == step and not crashed:
            crashed.append(name)
            raise RuntimeError("crash")

    purge.slack.hook = purge.hook = hook
    for _ in range(6):
        purge.tick()
        if crashed:
            break
    purge.slack.hook = purge.hook = None
    assert crashed == [step]
    if step in {"slack", "slack_tombstone"}:  # nothing was destroyed yet
        assert counts(stack, a.org_id, TEAM_A)[0] == 1
    drain(stack)
    assert deleted(stack, a)
    assert counts(stack, a.org_id, TEAM_A) == (0, 0, 0)
    assert counts(stack, b.org_id, TEAM_B) == (1, 0, 1)


def test_an_unwritable_tombstone_log_destroys_nothing_until_it_recovers(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, a = stack.world, stack.world.alpha
    lifecycle = world.runtime.slack_lifecycle
    real = lifecycle.tombstones

    class Refusing:
        def record(self, *_a: object, **_k: object) -> None:
            raise TombstoneWriteError("disk full")

    lifecycle.tombstones = Refusing()
    request_purge(stack, a, "Alpha")
    drain(stack, ticks=4)
    assert not deleted(stack, a)
    assert counts(stack, a.org_id, TEAM_A) == (1, 0, 1)  # row and secret survive
    assert "auth.revoke" not in world.api.methods()  # nothing irreversible happened
    lifecycle.tombstones = real
    drain(stack)
    assert deleted(stack, a) and counts(stack, a.org_id, TEAM_A) == (0, 0, 0)


def test_the_purge_works_when_slack_is_down(stack: Stack) -> None:
    stack.world.api.errors["auth.revoke"] = "ratelimited"
    request_purge(stack, stack.world.alpha, "Alpha")
    drain(stack)
    assert deleted(stack, stack.world.alpha)
    assert counts(stack, stack.world.alpha.org_id, TEAM_A) == (0, 0, 0)


def test_quarantine_suspends_drops_events_and_release_restores(stack: Stack) -> None:
    world, a, b = stack.world, stack.world.alpha, stack.world.beta
    service = quarantine_service(stack)
    view = service.quarantine(a.org_id, "ops@weaves.test", "abuse report")
    assert view["quarantine"]["slack_suspended"] == 1
    assert installation_status(stack, a) == "suspended"
    assert installation_status(stack, b) == "active"
    world.api.calls.clear()
    world.deliver(mention(event_id="Ev0QUAR01"))
    world.deliver(mention(team=TEAM_B, event_id="Ev0QUAR02"))
    rows = {e.event_id: e for e in world.runtime.slack_store.list_events()}
    assert (rows["Ev0QUAR01"].state, rows["Ev0QUAR01"].drop_reason) == (
        "dropped",
        "installation_suspended",
    )
    assert rows["Ev0QUAR01"].text is None
    assert rows["Ev0QUAR02"].state == "pending"  # tenant B is unaffected
    assert world.api.methods() == ["reactions.add"]  # the bot answers nothing for A
    # Idempotent, then reversible: only what quarantine suspended comes back.
    service.quarantine(a.org_id, "ops@weaves.test", "again")
    released = service.release(a.org_id, "ops@weaves.test", "cleared")
    assert released["state"] == "active"
    assert installation_status(stack, a) == "active"
    world.deliver(mention(event_id="Ev0QUAR03", ts="1700000099.000100"))
    assert world.runtime.slack_store.list_events()[-1].state == "pending"
    actions = [
        e.action
        for e in audit_rows(stack, a)
        if e.action.startswith("slack.installation.")
    ]
    assert "slack.installation.suspended" in actions
    assert "slack.installation.resumed" in actions


def test_quarantine_does_not_resume_an_installation_an_admin_ended(
    stack: Stack,
) -> None:
    a = stack.world.alpha
    service = quarantine_service(stack)
    service.quarantine(a.org_id, "ops@weaves.test", "abuse report")
    stack.world.deliver(lifecycle_event("app_uninstalled"))  # a suspended row ends
    service.release(a.org_id, "ops@weaves.test", "cleared")
    assert installation_status(stack, a) == "uninstalled"


def test_quarantine_with_revocation_ends_the_token_for_good(stack: Stack) -> None:
    world, a, b = stack.world, stack.world.alpha, stack.world.beta
    service = quarantine_service(stack)
    view = service.quarantine(
        a.org_id, "ops@weaves.test", "key leaked", revoke_credentials=True
    )
    assert view["quarantine"]["slack_revoked"] == 1
    assert installation_status(stack, a) == "revoked"
    assert counts(stack, a.org_id, TEAM_A)[2] == 0
    assert BOT_TOKEN in world.api.tokens  # revoked at Slack
    service.release(a.org_id, "ops@weaves.test", "cleared")
    assert installation_status(stack, a) == "revoked"  # never revived
    assert installation_status(stack, b) == "active"
    kinds = [t.kind for t in stack.log.read()]
    assert "slack_installation_ended" in kinds and "secret_deleted" in kinds


def test_another_organization_installs_after_uninstall_on_postgres(
    stack: Stack,
) -> None:
    world = stack.world
    world.deliver(lifecycle_event("app_uninstalled"))
    assert world.runtime.slack_store.find_by_team(TEAM_A) is None
    world.api.access = access_payload(team=TEAM_A, token="xoxb-takeover-token")
    done = world.install(world.beta, code="c-new")
    assert "slack_install=ok" in done.headers["location"]
    assert sql(
        stack.url,
        "SELECT org_id, status FROM slack_installations WHERE team_id=%s "
        "ORDER BY status",
        TEAM_A,
    ) == [(world.beta.org_id, "active"), (world.alpha.org_id, "uninstalled")]
    world.deliver(mention(event_id="Ev0NEWORG"))
    (row,) = [
        e for e in world.runtime.slack_store.list_events() if e.event_id == "Ev0NEWORG"
    ]
    assert row.org_id == world.beta.org_id


def test_no_secret_in_any_slack_row_audit_or_log_after_the_lifecycle(
    stack: Stack,
) -> None:
    world, a = stack.world, stack.world.alpha
    world.deliver(mention())
    quarantine_service(stack).quarantine(a.org_id, "ops@weaves.test", "x")
    world.deliver(lifecycle_event("app_uninstalled"))
    dump = json.dumps(
        sql(stack.url, "SELECT * FROM slack_installations")
        + sql(stack.url, "SELECT * FROM slack_event_inbox")
        + sql(
            stack.url,
            "SELECT payload::text FROM product_records "
            "WHERE collection = 'audit_events'",
        )
        + sql(stack.url, "SELECT * FROM operator_actions"),
        default=str,
    )
    for secret in (BOT_TOKEN, BETA_TOKEN):
        assert secret not in dump


def test_the_purge_of_one_organization_cannot_delete_another_ones_row(
    stack: Stack,
) -> None:
    world = stack.world
    store = world.runtime.slack_store
    token = set_request_identity("p", world.beta.org_id, world.beta.workspace_id)
    try:
        (mine,) = store.list_for_org()
    finally:
        reset_request_principal(token)
    token = set_request_identity("p", world.alpha.org_id, world.alpha.workspace_id)
    try:
        assert store.delete_installation(mine.installation_id) is False
    finally:
        reset_request_principal(token)
    assert counts(stack, world.beta.org_id, TEAM_B)[0] == 1


# -- retention on a bare store ------------------------------------------------


@pytest.fixture
def bare(isolated_postgres_url):
    pg = ProductPostgresStore(
        pool=open_postgres_pool(isolated_postgres_url, min_size=1, max_size=4),
        app_role="weaves_app",
    )
    yield PostgresSlackStore(pg)
    pg.close()
    require_tenant_context(False)


def aged(event_id: str, age_days: float, state: str = "done") -> Any:
    from dataclasses import replace

    return replace(
        event(event_id, ts=f"{zlib.crc32(event_id.encode()) % 10**6}.1"),
        received_at=NOW - timedelta(days=age_days),
        state=state,
    )


def test_retention_is_bounded_and_keeps_what_is_still_young(
    bare: PostgresSlackStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "BATCH", 4)
    monkeypatch.setattr(retention, "MAX_BATCHES", 2)
    for index in range(12):
        bare.record_event(aged(f"EvOld{index:02}", 8))  # processed, past 7 days
    bare.record_event(aged("EvYoung", 2))
    bare.record_event(aged("EvPendingYoung", 9, "pending"))  # unprocessed, under 14
    bare.record_event(aged("EvPendingOld", 20, "pending"))
    lifecycle = SlackLifecycle(
        store=bare,
        secrets=InMemorySecretStore(),
        api=None,  # type: ignore[arg-type]
        audit_events=None,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    first = retention.purge_inbox(lifecycle)
    assert first == 8 + 1 and len(bare.list_events(limit=100)) == 6  # two batches
    while retention.purge_inbox(lifecycle):
        pass
    assert sorted(e.event_id for e in bare.list_events(limit=100)) == [
        "EvPendingYoung",
        "EvYoung",
    ]
    assert retention.purge_inbox(lifecycle) == 0  # idempotent


def test_drop_pending_for_team_erases_text_and_spares_other_teams(
    bare: PostgresSlackStore,
) -> None:
    bare.record_event(event("EvA", ts="1.1", team="T0A"))
    bare.record_event(event("EvB", ts="2.2", team="T0B"))
    assert bare.drop_pending_for_team("T0A", "installation_ended", NOW) == 1
    states = {e.event_id: (e.state, e.drop_reason, e.text) for e in bare.list_events()}
    assert states == {
        "EvA": ("dropped", "installation_ended", None),
        "EvB": ("pending", None, "hello"),
    }


def test_list_unfinished_finds_only_ended_rows_that_still_hold_a_token(
    bare: PostgresSlackStore,
) -> None:
    bare.claim(installation("org-a", "T0A"))
    bare.claim(installation("org-b", "T0B"))
    token = set_request_identity("p", "org-a", "ws")
    try:
        bare.set_status("inst-org-a-T0A", "uninstalled", NOW)
    finally:
        reset_request_principal(token)
    assert [r.installation_id for r in bare.list_unfinished()] == ["inst-org-a-T0A"]
    token = set_request_identity("p", "org-a", "ws")
    try:
        bare.set_status("inst-org-a-T0A", "uninstalled", NOW, clear_secret=True)
    finally:
        reset_request_principal(token)
    assert bare.list_unfinished() == []


# -- helpers -------------------------------------------------------------------


def quarantine_service(stack: Stack) -> QuarantineService:
    runtime = stack.operator
    return QuarantineService(
        store=runtime.controls_store,
        audit=runtime.operator_audit,
        lifecycle=runtime.organization_lifecycle_service,
        schedules=runtime.workflow_schedules,
        triggers=runtime.workflow_triggers,
        credentials=runtime.api_credentials,
        tombstones=stack.operator.slack_lifecycle.tombstones,
        slack=runtime.slack_lifecycle,
        unit_of_work=runtime._unit_of_work,
    )


def installation_status(stack: Stack, org: Any) -> str:
    return str(
        sql(
            stack.url,
            "SELECT status FROM slack_installations WHERE org_id = %s",
            org.org_id,
        )[0][0]
    )


def audit_rows(stack: Stack, org: Any) -> list[Any]:
    token = set_request_identity("p", org.org_id, org.workspace_id)
    try:
        return list(stack.operator.audit_events.list_scoped(org.org_id))
    finally:
        reset_request_principal(token)
