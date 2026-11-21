"""Zoned schedules fire exactly once on PostgreSQL under the restricted role."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Barrier
from zoneinfo import ZoneInfo

import httpx
import pytest

from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import WorkflowSchedule
from contracts.v1.workflow_studio import Calendar
from infrastructure.database.pool import open_postgres_pool
from infrastructure.database.store import ProductPostgresStore
from modules.workflows.schedule_time import next_occurrences
from shared.request_context import require_tenant_context, system_scope
from tests.support.operator_scripts import backfill_schedule_timezones as script
from tests.support.operator_scripts import run_script
from tests.support.schedule_backfill_world import seed

ZONE = "America/New_York"


def open_restricted(url: str):
    pool = open_postgres_pool(url, min_size=1, max_size=4)
    store = ProductPostgresStore(pool=pool, app_role="weaves_app")
    runtime = compose_platform_runtime(http_client=httpx.Client(), store=store)
    runtime.bootstrap()
    return runtime


@pytest.fixture
def pair(isolated_postgres_url):
    first = open_restricted(isolated_postgres_url)
    second = open_restricted(isolated_postgres_url)
    yield first, second
    second.close()
    first.close()
    require_tenant_context(False)


def run_together(jobs):
    start = Barrier(len(jobs) + 1)

    def wrapped(job):
        start.wait(timeout=10)
        return job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = [executor.submit(wrapped, job) for job in jobs]
        start.wait(timeout=10)
        return [future.result(timeout=30) for future in futures]


def test_interval_schedule_with_a_zone_dispatches_once_on_two_runtimes(pair):
    first, second = pair
    schedule = first.create_workflow_schedule(
        "local-assistant-workflow",
        name="Zoned digest",
        task_instructions="Summarize.",
        interval_seconds=60,
        timezone=ZONE,
    )
    assert schedule.timezone == ZONE
    now = datetime.now(UTC)
    due = WorkflowSchedule.model_validate(
        schedule.model_copy(
            update={"next_run_at": now - timedelta(seconds=1)}
        ).model_dump()
    )
    first.workflow_schedules.put(due)
    results = run_together(
        [partial(r.dispatch_due_workflow_schedules, now=now) for r in (first, second)]
    )
    assert sum(len(jobs) for jobs in results) == 1
    for _ in range(3):
        assert first.dispatch_due_workflow_schedules(now=now) == ()
    stored = second.workflow_schedules.get(schedule.schedule_id)
    assert stored.timezone == ZONE and stored.last_run_at == due.next_run_at
    assert stored.next_run_at == now + timedelta(seconds=60)


def test_calendar_tick_fires_once_per_occurrence_across_workers(pair):
    first, second = pair
    studio = first.workflow_studio_service
    plan = {
        "steps": [
            {"id": "one", "type": "agent", "a": ["local-assistant"], "t": "Do it"}
        ],
        "sched": True,
        "calendar": {"freq": "daily", "days": [0, 1, 2, 3, 4, 5, 6], "time": "09:00"},
    }
    wid = studio.publish("Zoned calendar", "d", plan, None, ZONE)["definition"][
        "workflow_id"
    ]
    settings = studio.get_settings(wid)
    assert settings.timezone == ZONE and settings.next_run_at is not None
    due = settings.model_copy(
        update={"next_run_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    studio.settings.put(due)
    run_together([r.workflow_studio_service.tick for r in (first, second)] * 2)
    for _ in range(3):
        second.workflow_studio_service.tick()
    runs = studio.list_runs(wid)
    assert len(runs) == 1 and runs[0].trigger_type == "schedule"
    after = studio.get_settings(wid)
    assert after.next_run_at is not None and after.next_run_at > datetime.now(UTC)
    local = after.next_run_at.astimezone(ZoneInfo(ZONE))
    assert (local.hour, local.minute) == (9, 0)
    rule = Calendar.model_validate(after.calendar)
    assert after.last_run_at is not None
    assert after.next_run_at == next_occurrences(rule, after.last_run_at, ZONE)[0]


def test_backfill_on_the_owner_connection_is_idempotent(isolated_postgres_url, capsys):
    owner_store = ProductPostgresStore(isolated_postgres_url, app_role="")
    owner = compose_platform_runtime(
        http_client=httpx.Client(), store=owner_store, seed_local_defaults=False
    )
    owner.bootstrap()
    try:
        seed(owner, "org-backfill")
        status, _out, logs = run_script(script, capsys, owner, "--apply")
        assert status == 0
        assert logs[-1]["workflow_schedules"]["updated"] == 1
        with system_scope("test"):
            zones = {s.schedule_id: s.timezone for s in owner.workflow_schedules.list()}
        assert zones == {"sched-legacy": "Asia/Kolkata", "sched-zoned": ZONE}
        _status, _out, logs = run_script(script, capsys, owner, "--apply")
        assert logs[-1]["canvas_settings"]["updated"] == 0
    finally:
        owner.close()
