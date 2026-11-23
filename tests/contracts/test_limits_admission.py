"""Admission arithmetic, modes, overrides and the 429 mapping (no database)."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ports.admission import (
    AGENTS,
    QUEUED_WORK,
    SEATS,
    TOOL_CALLS_PER_DAY,
)
from application.ports.limits import StaticLimitPolicy
from apps.api.limit_errors import install_limit_error_handlers
from contracts.v1.limits import OrganizationLimitOverride
from modules.limits.errors import LimitExceeded
from modules.limits.policy import OverridingLimitPolicy, apply_override
from modules.limits.service import AdmissionService, NullAdmission
from shared.clock import utc_now
from shared.limits_config import LimitsConfig


class FakeCounts:
    """Counts a test sets directly; records every query."""

    def __init__(self, **used: int) -> None:
        self.used = used
        self.global_total = 0
        self.queries: list[tuple[str, str]] = []

    def count(self, org_id: str, resource: str) -> int:
        self.queries.append((org_id, resource))
        return self.used.get(resource, 0)

    def global_queued(self) -> int:
        return self.global_total


def service(
    mode: str = "enforce", counts: FakeCounts | None = None, **settings: object
) -> tuple[AdmissionService, FakeCounts, list[str]]:
    locks: list[str] = []
    counts = counts or FakeCounts()
    config = LimitsConfig(mode=mode, **settings)  # type: ignore[arg-type]
    return (
        AdmissionService(StaticLimitPolicy(config), counts, locks.append),
        counts,
        locks,
    )


def test_below_the_cap_admits_and_locks_per_org_and_resource() -> None:
    admission, counts, locks = service(counts=FakeCounts(seats=24))
    admission.admit("org-a", SEATS)
    assert locks == ["limit:org-a:seats"]
    assert counts.queries == [("org-a", SEATS)]


def test_at_the_cap_enforce_refuses_with_own_numbers() -> None:
    admission, _, _ = service(counts=FakeCounts(seats=25))
    with pytest.raises(LimitExceeded) as caught:
        admission.admit("org-a", SEATS)
    refusal = caught.value
    assert (refusal.resource, refusal.limit, refusal.used) == (SEATS, 25, 25)
    assert refusal.retry_after_seconds is None  # a person must free a seat


def test_queued_work_refusal_says_when_to_retry() -> None:
    admission, _, _ = service(counts=FakeCounts(queued_work=50))
    with pytest.raises(LimitExceeded) as caught:
        admission.admit_work("org-a")
    assert caught.value.resource == QUEUED_WORK
    assert caught.value.retry_after_seconds == 30


def test_observe_logs_the_would_be_denial_and_admits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    admission, _, _ = service("observe", FakeCounts(agents=50))
    with caplog.at_level(logging.WARNING, logger="product.limits"):
        admission.admit("org-a", AGENTS)
    assert "would be exceeded (observe mode)" in caplog.text
    assert "org_id=org-a resource=agents limit=50 used=50" in caplog.text


def test_off_skips_counting_and_locking_entirely() -> None:
    admission, counts, locks = service("off", FakeCounts(seats=10_000))
    admission.admit("org-a", SEATS)
    admission.admit_work("org-a")
    admission.admit_tool_call("org-a")
    assert counts.queries == [] and locks == []


def test_pause_blocks_new_work_in_enforce_but_not_other_resources() -> None:
    admission, _, _ = service(admissions_paused=True)
    with pytest.raises(LimitExceeded) as caught:
        admission.admit_work("org-a")
    assert caught.value.resource == "admissions_paused"
    assert caught.value.limit is None and caught.value.used is None
    admission.admit("org-a", SEATS)  # unaffected


def test_pause_in_observe_mode_only_logs(caplog: pytest.LogCaptureFixture) -> None:
    admission, _, _ = service("observe", admissions_paused=True)
    with caplog.at_level(logging.WARNING, logger="product.limits"):
        admission.admit_work("org-a")
    assert "resource=admissions_paused" in caplog.text


def test_global_brake_refuses_without_other_tenants_numbers() -> None:
    counts = FakeCounts()
    counts.global_total = 500
    admission, _, _ = service(counts=counts)
    with pytest.raises(LimitExceeded) as caught:
        admission.admit_work("org-a")
    assert caught.value.resource == "service_capacity"
    assert caught.value.limit is None and caught.value.used is None
    assert "500" not in caught.value.message


def test_task_admission_checks_work_before_storage() -> None:
    admission, _, locks = service()
    admission.admit_task("org-a")
    assert locks == ["limit:org-a:queued_work", "limit:org-a:tasks"]


def test_tool_calls_are_budgeted_without_a_lock() -> None:
    admission, _, locks = service(counts=FakeCounts(tool_calls_per_day=2_000))
    with pytest.raises(LimitExceeded) as caught:
        admission.admit_tool_call("org-a")
    assert caught.value.resource == TOOL_CALLS_PER_DAY
    assert caught.value.retry_after_seconds == 3600
    assert locks == []


def test_a_missing_lock_is_tolerated_for_memory_storage() -> None:
    config = LimitsConfig(mode="enforce")
    admission = AdmissionService(StaticLimitPolicy(config), FakeCounts(), None)
    admission.admit("org-a", SEATS)


def test_null_admission_admits_everything() -> None:
    null = NullAdmission()
    null.admit("o", SEATS)
    null.admit_work("o")
    null.admit_task("o")
    null.admit_tool_call("o")


def test_usage_reports_only_the_callers_own_consumption() -> None:
    admission, _, _ = service(counts=FakeCounts(seats=3, queued_work=4))
    view = admission.usage("org-a")
    by_resource = {item.resource: item for item in view.limits}
    assert by_resource["seats"].used == 3 and by_resource["seats"].limit == 25
    assert by_resource["queued_work"].used == 4
    assert view.mode == "enforce" and not view.admissions_paused
    assert set(by_resource) == {
        "queued_work",
        "tasks",
        "seats",
        "schedules",
        "agents",
        "workflows",
        "tool_calls_per_day",
    }


def _override(**fields: int) -> OrganizationLimitOverride:
    now = utc_now()
    return OrganizationLimitOverride(
        org_id="org-a",
        updated_by="ops@example.test",
        reason="pilot",
        created_at=now,
        updated_at=now,
        **fields,
    )


def test_override_replaces_only_the_fields_it_sets() -> None:
    base = StaticLimitPolicy(LimitsConfig()).for_org("org-a")
    merged = apply_override(base, _override(seats=60, max_queued_per_org=200))
    assert (merged.seats, merged.max_queued_per_org) == (60, 200)
    assert merged.agents == base.agents
    assert replace(merged, seats=25, max_queued_per_org=50) == base


def test_policy_caches_the_override_and_survives_lookup_failure() -> None:
    now = [0.0]
    loads: list[str] = []

    def load(org_id: str) -> OrganizationLimitOverride | None:
        loads.append(org_id)
        return _override(seats=99)

    policy = OverridingLimitPolicy(
        StaticLimitPolicy(LimitsConfig()), load, ttl_seconds=30, clock=lambda: now[0]
    )
    assert policy.for_org("org-a").seats == 99
    assert policy.for_org("org-a").seats == 99
    assert loads == ["org-a"] and policy.cached("org-a") is not None
    now[0] = 31.0
    assert policy.cached("org-a") is None
    policy.for_org("org-a")
    assert loads == ["org-a", "org-a"]

    def broken(_: str) -> OrganizationLimitOverride | None:
        raise RuntimeError("database down")

    fallback = OverridingLimitPolicy(StaticLimitPolicy(LimitsConfig()), broken)
    assert fallback.for_org("org-a").seats == 25


def test_off_mode_never_reads_overrides() -> None:
    def load(_: str) -> OrganizationLimitOverride | None:
        raise AssertionError("must not be read when limits are off")

    policy = OverridingLimitPolicy(StaticLimitPolicy(LimitsConfig(mode="off")), load)
    assert not policy.for_org("org-a").active


def test_limit_exceeded_maps_to_429_with_code_and_retry_after() -> None:
    app = FastAPI()
    install_limit_error_handlers(app)

    @app.get("/full")
    def full() -> None:
        raise LimitExceeded("queued_work", limit=50, used=50, retry_after_seconds=30)

    @app.get("/seats")
    def seats() -> None:
        raise LimitExceeded("seats", limit=25, used=25)

    @app.get("/paused")
    def paused() -> None:
        raise LimitExceeded("admissions_paused", message="New work is paused.")

    client = TestClient(app)
    full_response = client.get("/full")
    assert full_response.status_code == 429
    assert full_response.json() == {
        "detail": "limit reached for queued_work",
        "code": "limit_exceeded",
        "resource": "queued_work",
        "limit": 50,
        "used": 50,
    }
    assert full_response.headers["Retry-After"] == "30"
    seats_response = client.get("/seats")
    assert seats_response.status_code == 429
    assert "Retry-After" not in seats_response.headers
    paused_response = client.get("/paused")
    assert paused_response.json() == {
        "detail": "New work is paused.",
        "code": "limit_exceeded",
        "resource": "admissions_paused",
    }
