"""Canvas publication and actual durable worker flow regressions."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import TaskTurnStatus
from shared.clock import utc_now
from shared.request_context import reset_request_principal, set_request_identity
from tests.support.runtime import LocalPlatformRuntime


def step(key, kind="agent", agents=None, fail="stop"):
    return {
        "id": key,
        "type": kind,
        "a": ["local-assistant"]
        if agents is None and kind == "agent"
        else agents or [],
        "t": "Summarize the request"
        if kind == "agent"
        else "Manager approves"
        if kind == "appr"
        else "If the prior result qualifies",
        "fail": fail,
    }


@pytest.fixture
def runtime():
    r = LocalPlatformRuntime(postgres_store=None)
    r.bootstrap()
    yield r
    r.close()


def publish(runtime, steps, **starts):
    result = runtime.workflow_studio_service.publish(
        "Canvas recipe", "Test workflow", {"steps": steps, **starts}
    )
    return result["definition"]["workflow_id"]


def advance(runtime, limit=40):
    for _ in range(limit):
        runtime.workflow_studio_service.tick()
        runtime.task_worker_service.process_next_turn("canvas-test-worker")


def test_canvas_repeated_agent_approval_resume_and_pinned_version(runtime):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("one"), step("approval", "appr"), step("three")])
    run = s.start(wid, "Summarize this work", key="same-launch")
    assert s.start(wid, "Summarize this work", key="same-launch").run_id == run.run_id
    advance(runtime, 4)
    waiting = s.get_run(wid, run.run_id)
    assert waiting.status == "awaiting_approval"
    assert len(runtime.tasks.list()) == 1
    s.decide(wid, run.run_id, "approve")
    advance(runtime, 5)
    done = s.get_run(wid, run.run_id)
    assert done.status == "succeeded"
    assert len(runtime.tasks.list()) == 2
    assert done.summary
    assert all(
        t.agent_version_id == done.agent_versions["local-assistant"]
        for t in runtime.tasks.list()
    )
    assert done.artifacts and done.artifacts[0]["provenance"]


def test_reject_and_stop_do_not_dispatch_later_steps(runtime):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("approval", "appr"), step("agent")])
    run = s.start(wid, "Approval first")
    advance(runtime, 1)
    s.decide(wid, run.run_id, "reject")
    advance(runtime, 3)
    assert s.get_run(wid, run.run_id).status == "cancelled"
    assert not runtime.tasks.list()
    second = s.start(wid, "Stop before dispatch")
    s.decide(wid, second.run_id, "stop")
    advance(runtime, 2)
    assert not runtime.tasks.list()


@pytest.mark.parametrize(
    "policy, expected, attempts",
    [("stop", "failed", 1), ("retry", "failed", 3), ("skip", "succeeded", 1)],
)
def test_failure_policies_are_executed_not_just_saved(
    runtime, policy, expected, attempts
):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("agent", fail=policy)])
    run = s.start(wid, "Test failure")

    def failure(*args, **kwargs):
        raise RuntimeError("provider-secret-must-not-leak")

    runtime.agent_execution_service.run_agent = failure
    advance(runtime, 12)
    result = s.get_run(wid, run.run_id)
    assert result.status == expected
    assert len(runtime.tasks.list()) == attempts
    assert "provider-secret-must-not-leak" not in result.model_dump_json()


def test_retry_preserves_completed_results_and_retries_failed_stage(runtime):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("one"), step("two")])
    run = s.start(wid, "Retry flow")
    advance(runtime, 1)
    s.tick()
    original = runtime.agent_execution_service.run_agent
    runtime.agent_execution_service.run_agent = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("fail")
    )
    advance(runtime, 3)
    assert s.get_run(wid, run.run_id).status == "failed"
    runtime.agent_execution_service.run_agent = original
    retry = s.start(wid, "Retry flow", retry=run.run_id)
    advance(runtime, 5)
    assert s.get_run(wid, retry.run_id).status == "succeeded"
    assert len(runtime.tasks.list()) == 3


def test_calendar_ist_weekdays_and_paused_schedule(runtime):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("one")], sched=True)
    settings = s.get_settings(wid)
    dates = s.next_dates({"freq": "weekly", "days": [4], "time": "18:00"})
    assert len(dates) == 3
    assert all(d.weekday() == 4 and d.hour == 18 for d in dates)
    assert dates[0].utcoffset() == timedelta(hours=5, minutes=30)
    settings.next_run_at = utc_now() - timedelta(seconds=1)
    s.settings.put(settings)
    s.tick()
    s.tick()
    assert len(s.list_runs(wid)) == 1
    assert s.list_runs(wid)[0].trigger_type == "schedule"
    s.save_settings(wid, {"sched": False})
    assert s.get_settings(wid).next_run_at is None


def test_canvas_http_immutable_versions_permissions_and_scope(runtime):
    with TestClient(create_app(runtime)) as client:
        created = client.post(
            "/api/v0/workflow-studio/publish",
            json={
                "name": "Canvas recipe",
                "description": "Real plan",
                "plan": {"steps": [step("one")]},
            },
        )
        assert created.status_code == 200, created.text
        first = created.json()
        wid = first["definition"]["workflow_id"]
        updated = client.post(
            "/api/v0/workflow-studio/publish",
            json={
                "workflow_id": wid,
                "name": "Canvas recipe",
                "description": "Changed",
                "plan": {"steps": [step("one"), step("two")]},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"]["version"] == 2
        snapshot = client.get(
            f"/api/v0/workflows/{wid}/versions/{first['version']['workflow_version_id']}"
        ).json()
        assert len(snapshot["configuration"]["canvas"]["steps"]) == 1
        test = client.post(f"/api/v0/workflows/{wid}/studio/test-webhook")
        assert test.json() == {"received": True, "run_started": False}
        assert client.get(f"/api/v0/workflows/{wid}/runs").json() == {"items": []}
        launched = client.post(
            f"/api/v0/workflows/{wid}/runs", json={"instructions": "Run"}
        )
        assert launched.status_code == 202, launched.text
    token = set_request_identity("local-developer", "other-org", "other-workspace")
    try:
        with pytest.raises((KeyError, PermissionError)):
            runtime.workflow_studio_service.get_run(wid, launched.json()["run_id"])
    finally:
        reset_request_principal(token)


def test_stop_cancels_a_queued_child_task(runtime):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("one"), step("two")])
    run = s.start(wid, "Stop queued task")
    s.tick()
    task = runtime.tasks.list()[0]
    s.decide(wid, run.run_id, "stop")
    assert (
        runtime.task_turns.get(task.latest_turn_id).status == TaskTurnStatus.CANCELLED
    )
    advance(runtime, 3)
    assert len(runtime.tasks.list()) == 1
    assert s.get_run(wid, run.run_id).status == "cancelled"


def test_parallel_stage_dispatches_all_members_before_continuing(runtime):
    original = runtime.agents.get("local-assistant")
    version = runtime.agent_versions.get(original.current_version_id)
    runtime.agent_versions.create(
        version.model_copy(
            update={"agent_version_id": "second-v1", "agent_id": "second"}
        )
    )
    runtime.agents.create(
        original.model_copy(
            update={
                "id": "second",
                "name": "Second agent",
                "current_version_id": "second-v1",
            }
        )
    )
    s = runtime.workflow_studio_service
    wid = publish(
        runtime, [step("parallel", "par", ["local-assistant", "second"]), step("after")]
    )
    run = s.start(wid, "Parallel execution")
    s.tick()
    assert len(runtime.tasks.list()) == 2
    runtime.task_worker_service.process_next_turn("one-worker")
    s.tick()
    assert s.get_run(wid, run.run_id).states[1]["status"] == "queued"
    advance(runtime, 7)
    assert s.get_run(wid, run.run_id).status == "succeeded"
    assert len(runtime.tasks.list()) == 3


@pytest.mark.parametrize("decision", [True, False])
def test_condition_uses_toolless_evaluation_and_prunes_the_next_branch(
    runtime, decision
):
    s = runtime.workflow_studio_service
    wid = publish(
        runtime,
        [step("source"), step("condition", "cond"), step("conditional"), step("join")],
    )
    original = runtime.agent_execution_service.model_gateway.complete

    def condition_model(request, binding):
        response = original(request, binding)
        if any("Evaluate this condition" in m.content for m in request.messages):
            return response.model_copy(
                update={"content": "true" if decision else "false"}
            )
        return response

    runtime.agent_execution_service.model_gateway.complete = condition_model
    run = s.start(wid, "Condition test")
    advance(runtime, 14)
    result = s.get_run(wid, run.run_id)
    assert result.status == "succeeded", result.error
    assert result.states[1]["matched"] is decision
    assert result.states[2]["status"] == ("succeeded" if decision else "skipped")
    conditional_task = runtime.tasks.get(result.states[1]["tasks"][0])
    evaluator = runtime.agent_versions.get(conditional_task.agent_version_id)
    assert not evaluator.allowed_capability_ids
    assert not evaluator.skills


def test_native_webhook_dispatch_is_real_idempotent_and_honors_pause(runtime):
    s = runtime.workflow_studio_service
    wid = publish(runtime, [step("one")], hook=True)
    trigger, secret = runtime.create_workflow_trigger(
        wid, name="payout.failed", task_instructions="Review the failed payout"
    )
    job, replayed = runtime.enqueue_workflow_trigger_event(
        trigger.trigger_id, secret, "delivery-1", {"payout_id": "p1"}
    )
    assert not replayed
    duplicate, replayed = runtime.enqueue_workflow_trigger_event(
        trigger.trigger_id, secret, "delivery-1", {"payout_id": "p1"}
    )
    assert replayed and duplicate.job_id == job.job_id
    advance(runtime, 5)
    assert s.get_run(wid, job.run_id).status == "succeeded"
    s.save_settings(wid, {"hook": False})
    with pytest.raises(ValueError):
        runtime.enqueue_workflow_trigger_event(
            trigger.trigger_id, secret, "delivery-2", {"payout_id": "p2"}
        )
