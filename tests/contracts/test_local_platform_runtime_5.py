from contracts.v1 import TaskTurnStatus
from shared.request_context import (
    effective_org_id,
    effective_workspace_id,
    reset_request_principal,
    set_request_identity,
)
from tests.support.runtime import LocalPlatformRuntime


def test_task_worker_uses_the_tasks_tenant_scope():
    runtime = LocalPlatformRuntime()
    organization, workspace, _, owner, _ = runtime.create_organization(
        name="Worker Tenant",
        email="owner@worker.example",
        display_name="Worker Owner",
    )
    identity = set_request_identity(owner.id, organization.id, workspace.id)
    try:
        agent = runtime.agent_service.create_agent(
            name="Tenant worker agent",
            instructions="Summarize the available workspace guide.",
        )
        queued = runtime.task_service.create_task(
            agent_id=agent.id,
            title="Tenant task",
            initial_message="Summarize the available workspace guide.",
        )
    finally:
        reset_request_principal(identity)

    completed = runtime.task_worker_service.process_next_turn("tenant-worker")

    assert completed is not None
    assert completed.task_id == queued.task_id
    assert completed.status is TaskTurnStatus.SUCCEEDED
    assert completed.org_id == organization.id
    assert completed.workspace_id == workspace.id
    assert effective_org_id() == "local-org"
    assert effective_workspace_id() == "local-workspace"
