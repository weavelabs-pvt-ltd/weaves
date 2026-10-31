from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import (
    Permission,
    WorkflowScheduleStatus,
)
from tests.support.runtime import LocalPlatformRuntime


def _create_due_schedule(runtime: LocalPlatformRuntime):
    schedule = runtime.create_workflow_schedule(
        "local-assistant-workflow",
        name="Daily issue summary",
        task_instructions="Summarize the open issue queue.",
        interval_seconds=60,
    )
    now = datetime.now(UTC)
    due = type(schedule).model_validate(
        schedule.model_copy(
            update={"next_run_at": now - timedelta(minutes=1)}
        ).model_dump()
    )
    runtime.workflow_schedules.put(due)
    return due, now


def test_due_schedule_enqueues_once_and_coalesces_missed_intervals():
    runtime = LocalPlatformRuntime()
    schedule, now = _create_due_schedule(runtime)

    first = runtime.dispatch_due_workflow_schedules(now=now)
    replay = runtime.dispatch_due_workflow_schedules(now=now)

    assert len(first) == 1
    assert replay == ()
    job = first[0]
    assert job.workflow_id == schedule.workflow_id
    assert job.schedule_id == schedule.schedule_id
    assert job.schedule_occurrence_at == schedule.next_run_at
    assert "Summarize the open issue queue" in job.task
    assert runtime.workflow_schedules.get(schedule.schedule_id).next_run_at == (
        now + timedelta(seconds=schedule.interval_seconds)
    )

    late = now + timedelta(hours=4)
    missed = runtime.dispatch_due_workflow_schedules(now=late)
    assert len(missed) == 1
    assert runtime.dispatch_due_workflow_schedules(now=late) == ()
    updated = runtime.workflow_schedules.get(schedule.schedule_id)
    assert updated.last_run_at == now + timedelta(seconds=schedule.interval_seconds)
    assert updated.next_run_at == late + timedelta(seconds=schedule.interval_seconds)


def test_schedule_permissions_are_rechecked_and_failures_are_recorded_safely():
    runtime = LocalPlatformRuntime()
    schedule, now = _create_due_schedule(runtime)
    actual_authorize = runtime.authorize

    def revoked(principal_id, permission):
        if permission is Permission.WORKFLOWS_RUN:
            raise PermissionError("The schedule creator's current role changed.")
        return actual_authorize(principal_id, permission)

    runtime.authorize = revoked
    assert runtime.dispatch_due_workflow_schedules(now=now) == ()
    failed = runtime.workflow_schedules.get(schedule.schedule_id)

    assert failed.status is WorkflowScheduleStatus.ERROR
    assert failed.last_error is not None
    assert failed.last_error.code == "schedule.permission_revoked"
    assert "role changed" not in failed.last_error.summary
    assert runtime.dispatch_due_workflow_schedules(now=now) == ()


def test_concurrent_in_memory_workers_queue_a_schedule_once():
    runtime = LocalPlatformRuntime()
    _schedule, now = _create_due_schedule(runtime)
    start = Barrier(3)

    def dispatch():
        start.wait(timeout=2)
        return runtime.dispatch_due_workflow_schedules(now=now)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(dispatch)
        second = executor.submit(dispatch)
        start.wait(timeout=2)
        results = first.result(timeout=2) + second.result(timeout=2)

    assert len(results) == 1


def test_schedule_api_manages_durable_schedule_state(monkeypatch):
    monkeypatch.setenv("WEAVES_API_TOKEN", "workflow-schedule-admin-token")
    runtime = LocalPlatformRuntime()
    # Exercise the PostgreSQL-only HTTP surface; repository durability is covered
    # by the separately gated PostgreSQL integration tests.
    runtime.storage_mode = "postgres"
    client = TestClient(create_app(runtime))
    headers = {"Authorization": "Bearer workflow-schedule-admin-token"}

    with client:
        created = client.post(
            "/api/v0/workflows/local-assistant-workflow/schedules",
            headers=headers,
            json={
                "name": "Hourly issue summary",
                "task_instructions": "Summarize new issues.",
                "interval_seconds": 3600,
            },
        )
        assert created.status_code == 201, created.text
        schedule_id = created.json()["schedule_id"]
        assert created.json()["status"] == "active"

        listed = client.get("/api/v0/workflow-schedules", headers=headers)
        assert listed.status_code == 200, listed.text
        assert any(item["schedule_id"] == schedule_id for item in listed.json())

        disabled = client.patch(
            f"/api/v0/workflow-schedules/{schedule_id}",
            headers=headers,
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"

        enabled = client.patch(
            f"/api/v0/workflow-schedules/{schedule_id}",
            headers=headers,
            json={"status": "active"},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["status"] == "active"
        assert enabled.json()["next_run_at"] > disabled.json()["next_run_at"]


def test_schedule_api_requires_postgres_for_durable_operation():
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        response = client.post(
            "/api/v0/workflows/local-assistant-workflow/schedules",
            json={
                "name": "Hourly issue summary",
                "task_instructions": "Summarize new issues.",
                "interval_seconds": 3600,
            },
        )
    assert response.status_code == 503
    assert "PostgreSQL-backed storage" in response.json()["detail"]


def test_schedule_contract_rejects_invalid_interval_and_status_combinations():
    runtime = LocalPlatformRuntime()
    with pytest.raises(ValueError, match="between 60 and 31536000"):
        runtime.create_workflow_schedule(
            "local-assistant-workflow",
            name="Too frequent",
            task_instructions="Run the check.",
            interval_seconds=30,
        )
