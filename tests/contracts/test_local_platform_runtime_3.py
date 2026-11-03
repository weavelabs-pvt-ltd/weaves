from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest

from application import IdempotencyConflict
from clients.model_providers.gateway import ModelGateway, ModelGatewayError
from contracts.v1 import (
    ErrorSummary,
    ExecutionJob,
    ExecutionJobStatus,
    ModelProviderType,
    ModelResponse,
    ModelUsage,
    Permission,
    RunStatus,
    WorkflowParallelGroup,
)
from tests.support.runtime import LocalPlatformRuntime


def test_workflow_cancellation_stops_between_steps(monkeypatch):
    runtime = LocalPlatformRuntime()
    first = runtime.agent_service.create_agent(
        name="Cancel first", instructions="Prepare a plan."
    )
    second = runtime.agent_service.create_agent(
        name="Cancel second", instructions="Review the plan."
    )
    workflow = runtime.create_workflow(
        name="Cancellable workflow", agent_ids=(first.id, second.id)
    )
    cancellation_requested = Event()
    original_step = runtime._execute_agent_run
    executed_steps = []

    def execute_first_step(*args, **kwargs):
        result = original_step(*args, **kwargs)
        executed_steps.append(result)
        cancellation_requested.set()
        return result

    monkeypatch.setattr(runtime, "_execute_agent_run", execute_first_step)
    result = runtime.run_workflow(
        workflow.workflow_id,
        "Prepare and review a release",
        _cancellation_event=cancellation_requested,
    )

    assert len(executed_steps) == 1
    assert len(result.workflow_agent_runs) == 1
    assert result.run.status is RunStatus.CANCELLED
    assert result.run.finished_at is not None
    assert (
        sum(
            event.action == "workflow.run.cancelled"
            and event.target_id == result.run.run_id
            for event in runtime.audit_events.list()
        )
        == 1
    )


def test_worker_checks_persisted_workflow_cancellation_between_steps(monkeypatch):
    runtime = LocalPlatformRuntime()
    first = runtime.agent_service.create_agent(
        name="Persisted cancel first", instructions="Plan."
    )
    second = runtime.agent_service.create_agent(
        name="Persisted cancel second", instructions="Review."
    )
    workflow = runtime.create_workflow(
        name="Persisted cancellation workflow", agent_ids=(first.id, second.id)
    )
    job, _ = runtime.enqueue_agent_run(
        "Plan and review a change", workflow_id=workflow.workflow_id
    )
    original_step = runtime._execute_agent_run
    executed_steps = []

    def cancel_after_first_step(*args, **kwargs):
        result = original_step(*args, **kwargs)
        executed_steps.append(result)
        runtime.cancel_execution_job(job.job_id)
        return result

    monkeypatch.setattr(runtime, "_execute_agent_run", cancel_after_first_step)
    monkeypatch.setattr(runtime, "_renew_execution_job_lease", lambda *args: None)

    completed = runtime.process_next_execution_job("workflow-cancel-worker")

    assert completed is not None
    assert completed.status is ExecutionJobStatus.CANCELLED
    assert completed.run_id is not None
    workflow_run = runtime.runs.get(completed.run_id)
    assert workflow_run.status is RunStatus.CANCELLED
    assert len(executed_steps) == 1


def test_parallel_workflow_cancellation_waits_for_started_branches(monkeypatch):
    runtime = LocalPlatformRuntime()
    agents = tuple(
        runtime.agent_service.create_agent(
            name=f"Parallel cancellation {index}",
            instructions="Complete the assigned review step.",
            allowed_capability_ids=(),
        )
        for index in range(4)
    )
    workflow = runtime.create_workflow(
        name="Parallel cancellation workflow",
        agent_ids=tuple(agent.id for agent in agents),
    )
    runtime.replace_workflow_agents(
        workflow.workflow_id,
        tuple(agent.id for agent in agents),
        parallel_groups=(
            WorkflowParallelGroup(agent_ids=(agents[1].id, agents[2].id)),
        ),
    )
    job, _ = runtime.enqueue_agent_run(
        "Review the change", workflow_id=workflow.workflow_id
    )
    branch_barrier = Barrier(2)
    original_step = runtime._execute_agent_run

    def cancel_during_group(*args, **kwargs):
        result = original_step(*args, **kwargs)
        step_index = kwargs.get("workflow_step_index")
        if step_index in {1, 2}:
            branch_barrier.wait(timeout=5)
            if step_index == 1:
                runtime.cancel_execution_job(job.job_id)
        return result

    monkeypatch.setattr(runtime, "_execute_agent_run", cancel_during_group)
    monkeypatch.setattr(runtime, "_renew_execution_job_lease", lambda *args: None)

    completed = runtime.process_next_execution_job("parallel-cancel-worker")

    assert completed is not None
    assert completed.status is ExecutionJobStatus.CANCELLED
    assert completed.run_id is not None
    workflow_run = runtime.runs.get(completed.run_id)
    assert workflow_run.status is RunStatus.CANCELLED
    agent_runs = [
        item for item in runtime.agent_runs.list() if item.run_id == workflow_run.run_id
    ]
    assert {item.agent_id for item in agent_runs} == {
        agents[0].id,
        agents[1].id,
        agents[2].id,
    }


def test_retryable_execution_job_retries_with_lineage_and_idempotency():
    runtime = LocalPlatformRuntime()
    original, _ = runtime.enqueue_agent_run("Retry this failed request")
    started = original.updated_at
    finished = started + timedelta(seconds=1)
    failed = ExecutionJob.model_validate(
        original.model_copy(
            update={
                "status": ExecutionJobStatus.FAILED,
                "attempts": 1,
                "started_at": started,
                "finished_at": finished,
                "updated_at": finished,
                "error": ErrorSummary(
                    code="provider.timeout",
                    summary="The model provider timed out.",
                    retryable=True,
                ),
            }
        ).model_dump()
    )
    runtime.execution_jobs.put(failed)

    retry, replayed = runtime.retry_execution_job(
        original.job_id, idempotency_key="retry-request-1"
    )
    assert replayed is False
    assert retry.status is ExecutionJobStatus.QUEUED
    assert retry.retry_of_job_id == original.job_id
    assert retry.task == original.task
    assert retry.agent_id == original.agent_id

    replay, replayed = runtime.retry_execution_job(
        original.job_id, idempotency_key="retry-request-1"
    )
    assert replayed is True
    assert replay.job_id == retry.job_id

    completed = runtime.process_next_execution_job("retry-worker")
    assert completed is not None
    assert completed.job_id == retry.job_id
    assert completed.status is ExecutionJobStatus.SUCCEEDED
    assert (
        runtime.execution_jobs.get(original.job_id).status is ExecutionJobStatus.FAILED
    )
    assert any(
        event.action == "agent.execution_job.retry_queued"
        and event.target_id == retry.job_id
        for event in runtime.audit_events.list()
    )


def test_non_retryable_execution_job_cannot_be_retried():
    runtime = LocalPlatformRuntime()
    original, _ = runtime.enqueue_agent_run("Do not retry this request")
    started = original.updated_at
    finished = started + timedelta(seconds=1)
    runtime.execution_jobs.put(
        ExecutionJob.model_validate(
            original.model_copy(
                update={
                    "status": ExecutionJobStatus.FAILED,
                    "attempts": 1,
                    "started_at": started,
                    "finished_at": finished,
                    "updated_at": finished,
                    "error": ErrorSummary(
                        code="agent.configuration_invalid",
                        summary="The agent configuration is invalid.",
                        retryable=False,
                    ),
                }
            ).model_dump()
        )
    )

    with pytest.raises(ValueError, match="not marked retryable"):
        runtime.retry_execution_job(original.job_id, idempotency_key="retry-request-2")


def test_concurrent_api_credential_rotation_leaves_one_active_replacement():
    runtime = LocalPlatformRuntime()
    principal, _, original, _ = runtime.create_service_account(
        name="Rotatable service",
        permissions=(Permission.AGENTS_RUN,),
    )

    def rotate() -> bool:
        try:
            runtime.rotate_api_credential(original.id)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: rotate(), range(2)))

    assert sum(outcomes) == 1
    credentials = runtime.api_credentials.list_scoped(principal.org_id)
    active = [
        item
        for item in credentials
        if item.principal_id == principal.id and item.status.value == "active"
    ]
    assert len(active) == 1
    assert runtime.api_credentials.get(original.id).status.value == "revoked"


def test_execution_job_worker_runs_a_published_workflow():
    runtime = LocalPlatformRuntime()
    first_agent = runtime.agent_service.create_agent(
        name="Workflow planner",
        instructions="Plan the requested release review.",
    )
    second_agent = runtime.agent_service.create_agent(
        name="Workflow reviewer",
        instructions="Review the plan and identify risks.",
    )
    workflow = runtime.create_workflow(
        name="Queued release review", agent_ids=(first_agent.id, second_agent.id)
    )
    job, replayed = runtime.enqueue_agent_run(
        "Review the latest release",
        workflow_id=workflow.workflow_id,
        idempotency_key="queued-workflow-request",
    )

    assert replayed is False
    assert job.workflow_id == workflow.workflow_id
    assert job.workflow_version_id == workflow.current_version_id
    assert job.agent_id is None
    pinned_workflow_version_id = job.workflow_version_id
    pinned_first_agent_version_id = runtime.agents.get(
        first_agent.id
    ).current_version_id

    # A queued request must keep the published definition it accepted even if
    # the admin publishes a newer agent and workflow version before execution.
    runtime.agent_service.update_agent(
        first_agent.id, instructions="Plan the requested review with more detail."
    )
    assert (
        runtime.workflows.get(workflow.workflow_id).current_version_id
        != pinned_workflow_version_id
    )
    replay, replayed = runtime.enqueue_agent_run(
        "Review the latest release",
        workflow_id=workflow.workflow_id,
        idempotency_key="queued-workflow-request",
    )
    assert replayed is True
    assert replay.job_id == job.job_id
    with pytest.raises(IdempotencyConflict):
        runtime.enqueue_agent_run(
            "Review the latest release",
            agent_id=first_agent.id,
            idempotency_key="queued-workflow-request",
        )
    completed = runtime.process_next_execution_job("workflow-worker")

    assert completed is not None
    assert completed.status is ExecutionJobStatus.SUCCEEDED
    assert completed.workflow_id == workflow.workflow_id
    assert completed.run_id is not None
    run = runtime.runs.get(completed.run_id)
    assert run.workflow_id == workflow.workflow_id
    assert run.workflow_version_id == pinned_workflow_version_id
    agent_runs = tuple(
        item
        for item in runtime.agent_runs.list_scoped(run.org_id, run.workspace_id)
        if item.run_id == run.run_id
    )
    assert len(agent_runs) == 2
    assert [item.agent_id for item in agent_runs] == [first_agent.id, second_agent.id]
    assert agent_runs[0].agent_version_id == pinned_first_agent_version_id


def test_linear_workflow_failure_marks_parent_and_current_step_failed():
    runtime = LocalPlatformRuntime()
    first_agent = runtime.agent_service.create_agent(
        name="First step agent", instructions="Prepare a release plan."
    )
    second_agent = runtime.agent_service.create_agent(
        name="Second step agent", instructions="Review the release plan."
    )
    workflow = runtime.create_workflow(
        name="Failing linear workflow", agent_ids=(first_agent.id, second_agent.id)
    )

    class FailOnSecondCall:
        calls = 0

        def complete(self, request, binding):
            self.calls += 1
            if self.calls == 2:
                raise ModelGatewayError("test.failure", "Second step failed")
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content="First step result",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = FailOnSecondCall()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)

    with pytest.raises(ModelGatewayError, match="Second step failed"):
        runtime.run_workflow(workflow.workflow_id, "Review the release")

    run = runtime.runs.list()[0]
    agent_runs = tuple(
        sorted(
            (item for item in runtime.agent_runs.list() if item.run_id == run.run_id),
            key=lambda item: int(item.input_summary["workflow_step_index"]),
        )
    )
    assert run.status is RunStatus.FAILED
    assert run.error is not None
    assert len(agent_runs) == 2
    assert [item.status for item in agent_runs] == [
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
    ]


def test_execution_job_worker_claims_jobs_once_across_threads():
    runtime = LocalPlatformRuntime()
    jobs = [
        runtime.enqueue_agent_run(f"Queue request {index}")[0] for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = tuple(
            executor.map(
                lambda worker: runtime.execution_jobs.claim_next_queued(
                    worker,
                    datetime.now(UTC),
                    "local-org",
                    "local-workspace",
                ),
                ("worker-a", "worker-b"),
            )
        )
    assert {item.job_id for item in claimed if item is not None} == {
        job.job_id for job in jobs
    }
    assert all(item.status is ExecutionJobStatus.RUNNING for item in claimed)
