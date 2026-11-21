"""Indexed account lookups, bounded cleanup and tenant-visible link audit."""

from datetime import timedelta

import pytest

from contracts.v1 import AccountIdentity, AccountIdentityKind
from infrastructure.persistence.memory_repository import InMemoryRepository
from modules.auth.account_cleanup import PURGE_GRACE
from shared.clock import new_id, utc_now
from tests.support.account_rows import session_row, state_row
from tests.support.account_session import sign_in
from tests.support.account_world import seed_member, seed_organization
from tests.support.runtime import LocalPlatformRuntime

LONG_AGO = utc_now() - PURGE_GRACE - timedelta(minutes=5)
FIRST = "first.person@example.test"


def _identity(account_id, subject):
    now = utc_now()
    return AccountIdentity(
        id=new_id(),
        account_id=account_id,
        kind=AccountIdentityKind.EMAIL,
        subject=subject,
        verified_at=now,
        created_at=now,
        updated_at=now,
    )


def test_list_by_field_filters_orders_and_detaches():
    repo = InMemoryRepository("id")
    for ident in (
        _identity("acct-1", "b@example.test"),
        _identity("acct-1", "a@example.test"),
        _identity("acct-2", "c@example.test"),
    ):
        repo.create(ident)
    found = repo.list_by_field("account_id", "acct-1")
    assert [i.id for i in found] == sorted(i.id for i in found)
    assert {i.subject for i in found} == {"a@example.test", "b@example.test"}
    assert repo.list_by_field("account_id", "missing") == ()
    assert len(repo.list_by_field("kind", "email")) == 3
    assert len(repo.list_by_field("subject", "C@Example.test", casefold=True)) == 1
    assert repo.list_by_field("subject", "C@Example.test") == ()


def test_list_before_is_ordered_bounded_and_skips_missing_values():
    repo = InMemoryRepository("id")
    now = utc_now()
    rows = [
        session_row("a", now - timedelta(hours=3), revoked_at=now - timedelta(hours=1)),
        session_row("a", now - timedelta(hours=2)),
        session_row("a", now + timedelta(hours=1)),
    ]
    for row in rows:
        repo.create(row)
    expired = repo.list_before("expires_at", now, limit=10)
    assert [r.id for r in expired] == [rows[0].id, rows[1].id]
    assert [r.id for r in repo.list_before("expires_at", now, limit=1)] == [rows[0].id]
    assert [r.id for r in repo.list_before("revoked_at", now, limit=10)] == [rows[0].id]
    with pytest.raises(ValueError):
        repo.list_before("expires_at", now, limit=0)


@pytest.fixture
def world():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    service = runtime.account_sign_in_service
    yield runtime, service
    runtime.close()


def test_purge_removes_only_finished_rows_and_is_idempotent(world):
    _, service = world
    now = utc_now()
    states, sessions = service.oauth_states, service.sessions._sessions
    for i in range(2):
        states.create(state_row(f"d{i}", LONG_AGO))
    states.create(state_row("live", now + timedelta(minutes=5)))
    states.create(state_row("recent", now - timedelta(minutes=5)))
    expired = session_row("a", LONG_AGO)
    revoked = session_row("a", now + timedelta(hours=1), revoked_at=LONG_AGO)
    fresh_revoked = session_row("a", now + timedelta(hours=1), revoked_at=now)
    active = session_row("a", now + timedelta(hours=1))
    for row in (expired, revoked, fresh_revoked, active):
        sessions.create(row)

    assert service.purge_expired() == 4
    assert {c.return_to for c in states.list()} == {"/live", "/recent"}
    assert {s.id for s in sessions.list()} == {fresh_revoked.id, active.id}
    assert service.purge_expired() == 0


def test_purge_deletes_at_most_the_limit_per_call(world):
    _, service = world
    for i in range(5):
        service.oauth_states.create(state_row(f"x{i}", LONG_AGO))
    for _ in range(3):
        service.sessions._sessions.create(session_row("a", LONG_AGO))
    assert service.purge_expired(limit=3) == 3
    assert (
        len(service.oauth_states.list()) + len(service.sessions._sessions.list()) == 5
    )
    assert service.purge_expired(limit=3) == 3
    assert service.purge_expired(limit=3) == 2
    assert service.purge_expired(limit=3) == 0
    with pytest.raises(ValueError):
        service.purge_expired(limit=0)


def _sign_in(service, email):
    account, token = sign_in(service, email)
    return account, None, token


def _events(runtime, org_id):
    return [
        e
        for e in runtime.audit_events.list_scoped(org_id)
        if e.action == "account.membership.linked"
    ]


@pytest.fixture
def linked_world(world):
    runtime, service = world
    for org in ("org-a", "org-b"):
        seed_organization(runtime.organizations, org)
    _, principal = seed_member(runtime.users, runtime.principals, "org-a", FIRST)
    seed_member(runtime.users, runtime.principals, "org-b", "other@example.test")
    return runtime, service, principal


def test_linking_audits_only_in_the_linked_organization(linked_world):
    runtime, service, principal = linked_world
    account, _, _ = _sign_in(service, FIRST)
    (event,) = _events(runtime, "org-a")
    assert event.org_id == "org-a"
    assert event.actor_principal_id == principal.id
    assert event.metadata == {"account_id": account.id}
    assert FIRST not in event.model_dump_json()
    assert _events(runtime, "org-b") == []
    _sign_in(service, FIRST)  # relinking writes nothing new
    assert len(_events(runtime, "org-a")) == 1


def test_a_failed_audit_write_leaves_no_half_linked_membership(linked_world):
    runtime, service, _ = linked_world
    original = runtime.audit_events.create

    def broken(event):
        raise RuntimeError("audit store down")

    runtime.audit_events.create = broken
    with pytest.raises(RuntimeError):
        _sign_in(service, FIRST)
    assert runtime.memberships.list() == ()
    runtime.audit_events.create = original
    _sign_in(service, FIRST)
    assert len(runtime.memberships.list()) == 1
    assert len(_events(runtime, "org-a")) == 1
