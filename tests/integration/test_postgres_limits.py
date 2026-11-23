"""Atomic admission on PostgreSQL under the restricted application role.

Real threads over two independent runtimes (two pools, like two processes)
race at the cap; the stored row counts must never exceed it.
"""

from __future__ import annotations

import psycopg
import pytest

from modules.limits.errors import LimitExceeded
from tests.support.limits_world import CAP, build, race
from tests.support.operator_scripts import organization_limits, run_script
from tests.support.organization_administration import scope

OP = ("--operator", "ops@example.test", "--reason", "pg test")


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    built = build(isolated_postgres_url, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def test_racing_admissions_at_limit_minus_one_admit_exactly_one(world):
    for index in range(CAP - 1):
        world.create("a", f"fill-{index}")
    results = race(world, 12)
    admitted = [r for r in results if not isinstance(r, LimitExceeded)]
    refused = [r for r in results if isinstance(r, LimitExceeded)]
    assert len(admitted) == 1 and len(refused) == 11
    assert world.queued("a") == CAP
    assert {r.resource for r in refused} == {"queued_work"}


def test_racing_from_empty_never_exceeds_the_cap(world):
    results = race(world, 16)
    assert sum(not isinstance(r, LimitExceeded) for r in results) == CAP
    assert world.queued("a") == CAP


def test_a_full_organization_does_not_affect_another(world):
    race(world, 8)
    assert world.queued("a") == CAP
    task = world.create("b", "unaffected")
    assert task.task_id and world.queued("b") == 1


def test_an_idempotent_replay_at_the_limit_returns_the_existing_task(world):
    first = world.create("a", "kept", idempotency_key="same-key")
    for index in range(CAP - 1):
        world.create("a", f"fill-{index}")
    assert world.queued("a") == CAP
    again = world.create("a", "kept", idempotency_key="same-key")
    assert again.task_id == first.task_id
    with pytest.raises(LimitExceeded):
        world.create("a", "new", idempotency_key="other-key")


def test_the_same_idempotency_key_is_independent_per_tenant(world):
    for index in range(CAP):
        world.create("a", f"fill-{index}")
    in_b = world.create("b", "same", idempotency_key="shared-key")
    with pytest.raises(LimitExceeded):
        world.create("a", "same", idempotency_key="shared-key")
    assert in_b.org_id == world.b[0].id
    again = world.create("b", "same", idempotency_key="shared-key")
    assert again.task_id == in_b.task_id


def test_finishing_work_frees_capacity_without_any_counter(world):
    for index in range(CAP):
        world.create("a", f"fill-{index}")
    with pytest.raises(LimitExceeded):
        world.create("a", "blocked")
    turn = world.api.task_worker_service.process_next_turn("limits-worker")
    assert turn is not None
    assert world.queued("a") == CAP - 1
    assert world.create("a", "now fits").task_id


def test_a_refusal_writes_no_rows(world):
    for index in range(CAP):
        world.create("a", f"fill-{index}")
    with psycopg.connect(world.url, autocommit=True) as connection:
        before = connection.execute("SELECT count(*) FROM product_records").fetchone()[
            0
        ]
        with pytest.raises(LimitExceeded):
            world.create("a", "blocked")
        after = connection.execute("SELECT count(*) FROM product_records").fetchone()[0]
    assert before == after


def test_operator_override_raises_one_organization_and_is_audited(world, capsys):
    for index in range(CAP):
        world.create("a", f"fill-{index}")
    status, out, logs = run_script(
        organization_limits,
        capsys,
        world.operator,
        "set",
        "--organization-id",
        world.a[0].id,
        *OP,
        "--max-queued",
        "8",
    )
    assert status == 0 and '"max_queued_per_org": 8' in out
    assert logs[-1]["event"] == "operator.organization_limits"
    world.api.admission.policy.forget(world.a[0].id)
    world.api2.admission.policy.forget(world.a[0].id)
    assert world.create("a", "fits-now").task_id
    for index in range(CAP):  # the other organization keeps its default cap
        world.create("b", f"b-{index}")
    with pytest.raises(LimitExceeded):
        world.create("b", "b-over")
    with psycopg.connect(world.url, autocommit=True) as connection:
        audited = connection.execute(
            "SELECT payload->>'action', payload->>'actor_principal_id' "
            "FROM product_records WHERE collection = 'audit_events' "
            "AND payload->>'action' LIKE 'organization.limits.%'"
        ).fetchall()
    assert audited == [
        ("organization.limits.override_set", "operator:ops@example.test")
    ]
    run_script(
        organization_limits,
        capsys,
        world.operator,
        "clear",
        "--organization-id",
        world.a[0].id,
        *OP,
    )
    status, out, _ = run_script(
        organization_limits,
        capsys,
        world.operator,
        "show",
        "--organization-id",
        world.a[0].id,
    )
    assert status == 0 and '"override": null' in out


def test_the_restricted_role_reads_only_its_own_override(world, capsys):
    for org in (world.a, world.b):
        run_script(
            organization_limits,
            capsys,
            world.operator,
            "set",
            "--organization-id",
            org[0].id,
            *OP,
            "--seats",
            "7",
        )
    with scope(world.a):
        assert (
            world.api.organization_limits.get_scoped(world.a[0].id, world.a[0].id).seats
            == 7
        )
        with pytest.raises(KeyError):
            world.api.organization_limits.get_scoped(world.b[0].id, world.b[0].id)
    status, _, logs = run_script(
        organization_limits,
        capsys,
        world.operator,
        "set",
        "--organization-id",
        "no-such-org",
        *OP,
        "--seats",
        "7",
    )
    assert status == 2 and logs[-1]["event"].endswith("refused")


def test_the_global_brake_counts_every_tenant_without_reading_their_rows(
    isolated_postgres_url, monkeypatch
):
    built = build(
        isolated_postgres_url, monkeypatch, WEAVES_LIMIT_GLOBAL_MAX_QUEUED="3"
    )
    try:
        built.create("a", "a-1")
        built.create("a", "a-2")
        built.create("b", "b-1")
        with pytest.raises(LimitExceeded) as caught:
            built.create("b", "b-2")
        assert caught.value.resource == "service_capacity"
        assert caught.value.used is None and caught.value.limit is None
    finally:
        built.close()


def test_pausing_admissions_stops_new_work_only(isolated_postgres_url, monkeypatch):
    built = build(isolated_postgres_url, monkeypatch, WEAVES_ADMISSIONS_PAUSED="true")
    try:
        with pytest.raises(LimitExceeded) as caught:
            built.create("a", "paused")
        assert caught.value.resource == "admissions_paused"
        with scope(built.a):
            assert built.api.task_service.list_tasks() == ()
    finally:
        built.close()


def test_observe_mode_logs_and_admits(isolated_postgres_url, monkeypatch, caplog):
    built = build(isolated_postgres_url, monkeypatch, WEAVES_LIMITS_MODE="observe")
    try:
        for index in range(CAP + 2):
            built.create("a", f"over-{index}")
        assert built.queued("a") == CAP + 2
        assert "would be exceeded (observe mode)" in caplog.text
    finally:
        built.close()


def test_off_mode_never_counts(isolated_postgres_url, monkeypatch, caplog):
    built = build(isolated_postgres_url, monkeypatch, WEAVES_LIMITS_MODE="off")
    try:
        for index in range(CAP + 2):
            built.create("a", f"over-{index}")
        assert built.queued("a") == CAP + 2
        assert "would be exceeded" not in caplog.text
    finally:
        built.close()
