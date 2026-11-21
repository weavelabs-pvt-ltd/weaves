"""The operator backfill is dry by default, idempotent and non-destructive."""

import pytest

from shared.request_context import system_scope
from tests.support.operator_scripts import backfill_schedule_timezones as script
from tests.support.operator_scripts import run_script
from tests.support.runtime import LocalPlatformRuntime
from tests.support.schedule_backfill_world import seed


@pytest.fixture
def runtime():
    r = LocalPlatformRuntime(postgres_store=None)
    r.bootstrap()
    yield r
    r.close()


def zones(runtime):
    with system_scope("test"):
        return (
            {s.schedule_id: s.timezone for s in runtime.workflow_schedules.list()},
            {s.workflow_id: s.timezone for s in runtime.canvas_settings.list()},
        )


def summary(logs):
    return next(
        line for line in logs if line["event"] == "operator.backfill_schedule_timezones"
    )


def test_dry_run_is_the_default_and_writes_nothing(runtime, capsys):
    seed(runtime)
    before = zones(runtime)
    status, _out, logs = run_script(script, capsys, runtime)
    assert status == 0
    assert summary(logs)["mode"] == "dry-run"
    assert summary(logs)["workflow_schedules"] == {
        "total": 2,
        "legacy": 1,
        "updated": 0,
    }
    assert zones(runtime) == before


def test_apply_fills_only_legacy_records_and_is_idempotent(runtime, capsys):
    seeded = seed(runtime)
    status, _out, logs = run_script(script, capsys, runtime, "--apply")
    assert status == 0 and summary(logs)["canvas_settings"]["updated"] == 1
    schedules, settings = zones(runtime)
    assert schedules == {
        "sched-legacy": "Asia/Kolkata",
        "sched-zoned": "America/New_York",
    }
    assert settings == {"wf-legacy": "Asia/Kolkata", "wf-zoned": "Europe/London"}
    legacy = seeded["schedules"][0]
    with system_scope("test"):
        stored = runtime.workflow_schedules.get(legacy.schedule_id)
    assert stored.updated_at == legacy.updated_at
    assert stored.next_run_at == legacy.next_run_at
    again, _out, logs = run_script(script, capsys, runtime, "--apply")
    assert again == 0
    assert summary(logs)["workflow_schedules"]["updated"] == 0
    assert zones(runtime) == (schedules, settings)


def test_conflicting_flags_are_refused(runtime, capsys):
    status, _out, _logs = run_script(script, capsys, runtime, "--apply", "--dry-run")
    assert status == 2
