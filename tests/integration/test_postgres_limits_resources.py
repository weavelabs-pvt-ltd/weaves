"""Per-resource limits, the 429 contract and tool-call budgets on PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import Permission
from modules.limits.errors import LimitExceeded
from tests.support.limits_world import CAP, build
from tests.support.organization_administration import scope


@pytest.fixture
def world(isolated_postgres_url, monkeypatch):
    built = build(isolated_postgres_url, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def test_seats_schedules_agents_and_workflows_enforce_their_caps(
    isolated_postgres_url, monkeypatch
):
    built = build(
        isolated_postgres_url,
        monkeypatch,
        WEAVES_LIMIT_SEATS="2",
        WEAVES_LIMIT_SCHEDULES="1",
        WEAVES_LIMIT_AGENTS="2",
        WEAVES_LIMIT_WORKFLOWS="2",
    )
    try:
        with scope(built.a):
            service = built.api.user_administration_service
            service.create_workspace_user(
                email="m1@example.test",
                display_name="M1",
                permissions=(Permission.TASKS_VIEW,),
            )
            with pytest.raises(LimitExceeded) as seats:
                service.create_workspace_user(
                    email="m2@example.test",
                    display_name="M2",
                    permissions=(Permission.TASKS_VIEW,),
                )
            assert (seats.value.resource, seats.value.limit, seats.value.used) == (
                "seats",
                2,
                2,
            )
            agents = built.api.agent_service
            agents.create_agent(name="Second", instructions="x")
            with pytest.raises(LimitExceeded) as agent:
                agents.create_agent(name="Third", instructions="x")
            assert agent.value.resource == "agents"
            workflow_id = next(
                w.workflow_id for w in built.api.workflows.list_scoped(built.a[0].id)
            )
            automation = built.api.workflow_automation_service
            automation.create_workflow_schedule(
                workflow_id, name="One", task_instructions="x", interval_seconds=60
            )
            with pytest.raises(LimitExceeded) as schedule:
                automation.create_workflow_schedule(
                    workflow_id, name="Two", task_instructions="x", interval_seconds=60
                )
            assert schedule.value.resource == "schedules"
            with pytest.raises(LimitExceeded) as workflow:
                built.api.workflow_definition_service.create_workflow(
                    name="Flow", agent_id=built.agent["a"]
                )
            assert workflow.value.resource == "workflows"
        with scope(built.b):  # another organization is untouched
            built.api.agent_service.create_agent(name="B2", instructions="x")
    finally:
        built.close()


def test_http_returns_429_limit_exceeded_and_the_usage_view(
    isolated_postgres_url, monkeypatch
):
    built = build(isolated_postgres_url, monkeypatch)
    try:
        token = built.a[4]
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Workspace-ID": built.a[1].id,
        }
        body = {"agent_id": built.agent["a"], "title": "t", "initial_message": "hi"}
        with TestClient(create_app(built.api), raise_server_exceptions=False) as client:
            for index in range(CAP):
                created = client.post(
                    "/api/v0/tasks",
                    json={**body, "title": f"t{index}"},
                    headers=headers,
                )
                assert created.status_code == 202, created.text
            refused = client.post("/api/v0/tasks", json=body, headers=headers)
            assert refused.status_code == 429
            assert refused.json() == {
                "detail": "limit reached for queued_work",
                "code": "limit_exceeded",
                "resource": "queued_work",
                "limit": CAP,
                "used": CAP,
            }
            assert refused.headers["Retry-After"] == "30"
            view = client.get("/api/v0/organization/limits", headers=headers)
            assert view.status_code == 200
            usage = {item["resource"]: item for item in view.json()["limits"]}
            assert (usage["queued_work"]["used"], usage["queued_work"]["limit"]) == (
                CAP,
                CAP,
            )
            assert view.json()["mode"] == "enforce"
    finally:
        built.close()


def test_a_due_schedule_is_skipped_not_failed_when_the_queue_is_full(world):
    from datetime import timedelta

    from contracts.v1 import WorkflowSchedule, WorkflowScheduleStatus
    from shared.clock import utc_now

    automation = world.api.workflow_automation_service
    with scope(world.a):
        workflow_id = next(
            w.workflow_id for w in world.api.workflows.list_scoped(world.a[0].id)
        )
        schedule = automation.create_workflow_schedule(
            workflow_id, name="Due", task_instructions="x", interval_seconds=60
        )
    now = utc_now()

    def make_due() -> None:
        with scope(world.a):
            current = world.api.workflow_schedules.get(schedule.schedule_id)
            world.api.workflow_schedules.put(
                WorkflowSchedule.model_validate(
                    current.model_copy(
                        update={"next_run_at": now - timedelta(seconds=1)}
                    ).model_dump()
                )
            )

    for index in range(CAP):
        world.create("a", f"fill-{index}")
    make_due()
    assert automation.dispatch_due_workflow_schedules(now=now) == ()
    with scope(world.a):
        stored = world.api.workflow_schedules.get(schedule.schedule_id)
        actions = {e.action for e in world.api.audit_events.list_scoped(world.a[0].id)}
    assert stored.status is WorkflowScheduleStatus.ACTIVE
    assert stored.last_error is None and stored.next_run_at > now
    assert "workflow.schedule.limited" in actions
    assert world.queued("a") == CAP
    assert world.api.task_worker_service.process_next_turn("limits-worker")
    make_due()
    assert len(automation.dispatch_due_workflow_schedules(now=now)) == 1


def test_tool_calls_count_only_the_last_day_of_the_organization(
    isolated_postgres_url, monkeypatch
):
    from datetime import timedelta

    from contracts.v1 import InvocationStatus, RiskLevel, ToolInvocation
    from shared.clock import new_id, utc_now

    built = build(
        isolated_postgres_url, monkeypatch, WEAVES_LIMIT_TOOL_CALLS_PER_DAY="3"
    )
    try:

        def record(org_owner: Any, age_hours: int) -> None:
            stamp = utc_now() - timedelta(hours=age_hours)
            with scope(org_owner):
                built.api.tool_invocations.create(
                    ToolInvocation(
                        invocation_id=new_id(),
                        run_id=new_id(),
                        agent_run_id=new_id(),
                        org_id=org_owner[0].id,
                        workspace_id=org_owner[1].id,
                        plugin_installation_id="install",
                        capability_id="slack.messages.search",
                        status=InvocationStatus.SUCCEEDED,
                        risk_level=RiskLevel.LOW,
                        created_at=stamp,
                        updated_at=stamp,
                        started_at=stamp,
                        finished_at=stamp,
                        duration_ms=1,
                    )
                )

        for _ in range(2):
            record(built.a, 1)
        record(built.a, 30)  # older than a day: no longer counts
        record(built.b, 1)
        admission = built.api.admission
        with scope(built.a):
            admission.admit_tool_call(built.a[0].id)  # 2 of 3 used
            record(built.a, 1)
            with pytest.raises(LimitExceeded) as caught:
                admission.admit_tool_call(built.a[0].id)
        assert (caught.value.limit, caught.value.used) == (3, 3)
        with scope(built.b):
            admission.admit_tool_call(built.b[0].id)
    finally:
        built.close()
