"""Home uses task-authorized, persisted execution costs and sources."""

from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.support.runtime import LocalPlatformRuntime


def test_home_execution_metadata_uses_real_turns_and_keeps_default_task_shape():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    task = runtime.task_service.create_task(
        agent_id="local-assistant",
        title="Home metadata",
        initial_message="Summarize the handbook",
    )
    runtime.task_worker_service.process_next_turn("home-worker")
    turn = runtime.task_turns.get(task.latest_turn_id)
    run = runtime.agent_runs.get(turn.agent_run_id)
    runtime.agent_runs.put(
        run.model_copy(update={"estimated_cost_usd": Decimal("0.125")})
    )
    with TestClient(create_app(runtime)) as client:
        ordinary = client.get("/api/v0/tasks").json()["items"][0]
        assert "execution" not in ordinary
        enriched = client.get("/api/v0/tasks?include_execution=true").json()["items"][0]
        execution = enriched["execution"]
        assert execution["estimated_cost_usd"] == "0.125"
        assert execution["agent_run_id"] == turn.agent_run_id
        assert execution["turns"][0]["status"] == "succeeded"
        assert execution["started_at"] and execution["finished_at"]
        assert execution["source_ids"] == list(
            (run.output_summary or {}).get("source_ids", [])
        )
        assert "input_summary" not in execution
        assert "instructions" not in execution
        other = runtime.create_workspace(name="Other workspace")
        scoped = client.get(
            "/api/v0/tasks?include_execution=true", headers={"X-Workspace-ID": other.id}
        )
        assert scoped.status_code == 200
        assert scoped.json()["items"] == []


def test_unmetered_queued_turn_has_no_invented_cost():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    runtime.task_service.create_task(
        agent_id="local-assistant", title="Queued", initial_message="Summarize"
    )
    with TestClient(create_app(runtime)) as client:
        execution = client.get("/api/v0/tasks?include_execution=true").json()["items"][
            0
        ]["execution"]
        assert execution["estimated_cost_usd"] is None
        assert execution["turns"][0]["estimated_cost_usd"] is None
        assert execution["turns"][0]["status"] == "queued"
