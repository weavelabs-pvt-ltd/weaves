"""Schedule timezones through the API: defaults, validation, edits and firing."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import OrganizationSettings, WorkflowSchedule
from contracts.v1.workflow_studio import Calendar, CanvasSettings
from modules.workflows.schedule_time import next_occurrences
from shared.clock import utc_now
from tests.contracts.test_workflow_studio import publish, step
from tests.support.runtime import LocalPlatformRuntime

NEW_YORK, LONDON = "America/New_York", "Europe/London"
SCHEDULES = "/api/v0/workflow-schedules"
CREATE = "/api/v0/workflows/local-assistant-workflow/schedules"
BODY = {"name": "Digest", "task_instructions": "Summarize.", "interval_seconds": 3600}


@pytest.fixture
def runtime():
    r = LocalPlatformRuntime(postgres_store=None)
    r.bootstrap()
    r.storage_mode = "postgres"  # exposes the durable-only schedule HTTP surface
    yield r
    r.close()


@pytest.fixture
def client(runtime, monkeypatch):
    monkeypatch.setenv("WEAVES_API_TOKEN", "tz-admin-token")
    with TestClient(create_app(runtime)) as c:
        c.headers["Authorization"] = "Bearer tz-admin-token"
        yield c


def set_org_zone(runtime, zone: str) -> None:
    now = utc_now()
    runtime.organization_settings.put(
        OrganizationSettings(
            id="local-org",
            org_id="local-org",
            owner_principal_id="local-developer",
            timezone=zone,
            created_at=now,
            updated_at=now,
        )
    )


def test_new_interval_schedule_inherits_organization_zone_else_kolkata(runtime, client):
    assert client.post(CREATE, json=BODY).json()["timezone"] == "Asia/Kolkata"
    set_org_zone(runtime, NEW_YORK)
    created = client.post(CREATE, json=BODY)
    assert created.status_code == 201, created.text
    assert created.json()["timezone"] == NEW_YORK
    assert created.json()["next_run_at"].endswith("Z")
    named = client.post(CREATE, json={**BODY, "timezone": LONDON}).json()
    assert named["timezone"] == LONDON
    stored = runtime.workflow_schedules.get(named["schedule_id"])
    assert stored.timezone == LONDON


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", " ", "x" * 65, "../etc"])
def test_unknown_or_oversized_zone_is_a_422(client, zone):
    assert client.post(CREATE, json={**BODY, "timezone": zone}).status_code == 422
    created = client.post(CREATE, json=BODY).json()
    patch = client.patch(
        f"{SCHEDULES}/{created['schedule_id']}",
        json={"timezone": zone, "expected_updated_at": created["updated_at"]},
    )
    assert patch.status_code == 422


def test_legacy_schedule_without_a_zone_reads_as_kolkata(runtime, client):
    created = client.post(CREATE, json=BODY).json()
    legacy = WorkflowSchedule.model_validate(
        runtime.workflow_schedules.get(created["schedule_id"])
        .model_copy(update={"timezone": None})
        .model_dump()
    )
    runtime.workflow_schedules.put(legacy)
    listed = {i["schedule_id"]: i for i in client.get(SCHEDULES).json()}
    assert listed[legacy.schedule_id]["timezone"] == "Asia/Kolkata"
    assert listed[legacy.schedule_id]["next_run_at"] == created["next_run_at"]
    dumped = legacy.model_dump_json(exclude={"timezone"})
    assert WorkflowSchedule.model_validate_json(dumped).timezone is None


def test_interval_timezone_edit_needs_revision_and_keeps_pending_instant(
    runtime, client
):
    created = client.post(CREATE, json=BODY).json()
    url = f"{SCHEDULES}/{created['schedule_id']}"
    missing = client.patch(url, json={"timezone": LONDON})
    assert missing.status_code == 422
    stale = client.patch(
        url, json={"timezone": LONDON, "expected_updated_at": "2020-01-01T00:00:00Z"}
    )
    assert stale.status_code == 409
    ok = client.patch(
        url, json={"timezone": LONDON, "expected_updated_at": created["updated_at"]}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["timezone"] == LONDON
    assert ok.json()["next_run_at"] == created["next_run_at"]
    assert ok.json()["last_run_at"] is None
    again = client.patch(url, json={"timezone": LONDON})
    assert (
        again.status_code == 200
        and again.json()["updated_at"] == ok.json()["updated_at"]
    )
    audit = [
        e
        for e in runtime.audit_events.list()
        if e.target_id == created["schedule_id"] and e.action.endswith("updated")
    ]
    assert audit and audit[-1].metadata["timezone"] == LONDON


def settings_url(wid: str) -> str:
    return f"/api/v0/workflows/{wid}/studio/settings"


def publish_calendar(runtime, zone=None, **calendar):
    cal = {"freq": "daily", "days": [0, 1, 2, 3, 4, 5, 6], "time": "09:00", **calendar}
    plan = {"steps": [step("one")], "sched": True, "calendar": cal}
    result = runtime.workflow_studio_service.publish(
        f"Canvas {uuid4().hex[:8]}", "Test workflow", plan, None, zone
    )
    return result["definition"]["workflow_id"]


def test_calendar_defaults_to_org_zone_and_reports_utc_instants(runtime, client):
    set_org_zone(runtime, NEW_YORK)
    wid = publish_calendar(runtime)
    got = client.get(settings_url(wid)).json()
    assert got["timezone"] == NEW_YORK
    assert all(v.endswith(("-04:00", "-05:00")) for v in got["next_runs"])
    stored = runtime.workflow_studio_service.get_settings(wid)
    assert stored.next_run_at is not None and stored.next_run_at.tzinfo is not None
    assert stored.next_run_at.utcoffset() == timedelta(0)
    local = stored.next_run_at.astimezone(ZoneInfo(NEW_YORK))
    assert (local.hour, local.minute) == (9, 0)
    explicit = publish_calendar(runtime, LONDON)
    assert client.get(settings_url(explicit)).json()["timezone"] == LONDON


def test_legacy_calendar_settings_read_as_kolkata_with_old_instants(runtime, client):
    wid = publish_calendar(runtime)
    legacy = CanvasSettings.model_validate(
        runtime.workflow_studio_service.get_settings(wid)
        .model_copy(update={"timezone": None})
        .model_dump()
    )
    runtime.canvas_settings.put(legacy)
    got = client.get(settings_url(wid)).json()
    assert got["timezone"] == "Asia/Kolkata"
    assert all(v.endswith("+05:30") for v in got["next_runs"])
    expected = next_occurrences(Calendar(**legacy.calendar), utc_now(), None)
    assert [datetime.fromisoformat(v) for v in got["next_runs"]] == expected


def test_unknown_calendar_zone_is_rejected_by_publish_and_settings(runtime, client):
    wid = publish_calendar(runtime)
    for zone in ("Mars/Olympus", "x" * 65):
        patch = client.patch(settings_url(wid), json={"timezone": zone})
        assert patch.status_code == 422
        body = {"name": "Canvas recipe", "plan": {"steps": [step("one")]}}
        assert (
            client.post(
                "/api/v0/workflow-studio/publish", json={**body, "timezone": zone}
            ).status_code
            == 422
        )


def pending(runtime, wid) -> datetime:
    value = runtime.workflow_studio_service.get_settings(wid).next_run_at
    assert value is not None
    return value


def test_zone_change_keeps_the_pending_instant_and_needs_a_revision(runtime, client):
    wid = publish_calendar(runtime, NEW_YORK)
    before = client.get(settings_url(wid)).json()
    first = pending(runtime, wid)
    assert client.patch(settings_url(wid), json={"timezone": LONDON}).status_code == 422
    stale = client.patch(
        settings_url(wid),
        json={"timezone": LONDON, "expected_updated_at": "2020-01-01T00:00:00Z"},
    )
    assert stale.status_code == 409
    ok = client.patch(
        settings_url(wid),
        json={"timezone": LONDON, "expected_updated_at": before["updated_at"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["timezone"] == LONDON
    assert pending(runtime, wid) == first


def test_explicit_time_edit_with_zone_change_recomputes_from_now(runtime, client):
    wid = publish_calendar(runtime, NEW_YORK)
    before = client.get(settings_url(wid)).json()
    body = {
        "timezone": LONDON,
        "calendar": {"freq": "daily", "days": [0, 1, 2, 3, 4, 5, 6], "time": "21:15"},
        "expected_updated_at": before["updated_at"],
    }
    assert client.patch(settings_url(wid), json=body).status_code == 200
    moved = pending(runtime, wid)
    local = moved.astimezone(ZoneInfo(LONDON))
    assert (local.hour, local.minute) == (21, 15)
    assert utc_now() < moved <= utc_now() + timedelta(hours=25)


def test_unrelated_edits_keep_pending_and_disable_clears_it(runtime, client):
    wid = publish_calendar(runtime, NEW_YORK)
    first = pending(runtime, wid)
    assert client.patch(settings_url(wid), json={"manual": False}).status_code == 200
    assert pending(runtime, wid) == first
    client.patch(settings_url(wid), json={"sched": False})
    assert runtime.workflow_studio_service.get_settings(wid).next_run_at is None
    client.patch(settings_url(wid), json={"sched": True})
    assert pending(runtime, wid) > utc_now()


def test_zone_change_never_fires_early_or_twice(runtime, client):
    studio = runtime.workflow_studio_service
    wid = publish_calendar(runtime, NEW_YORK)
    current = studio.get_settings(wid)
    resp = client.patch(
        settings_url(wid),
        json={
            "timezone": LONDON,
            "expected_updated_at": current.updated_at.isoformat(),
        },
    )
    assert resp.status_code == 200
    studio.tick()
    assert studio.list_runs(wid) == []  # the pending instant is still in the future
    due = studio.get_settings(wid)
    due.next_run_at = utc_now() - timedelta(seconds=1)
    studio.settings.put(due)
    for _ in range(3):
        studio.tick()
    assert len(studio.list_runs(wid)) == 1
    after = pending(runtime, wid)
    assert after > utc_now()
    local = after.astimezone(ZoneInfo(LONDON))
    assert (local.hour, local.minute) == (9, 0)


def test_publish_helper_still_defaults_without_zone(runtime):
    wid = publish(runtime, [step("one")], sched=True)
    assert runtime.workflow_studio_service.get_settings(wid).timezone == "Asia/Kolkata"
