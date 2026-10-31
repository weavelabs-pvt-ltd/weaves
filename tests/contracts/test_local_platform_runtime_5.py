from contracts.v1 import (
    ExecutionJobStatus,
)
from shared.request_context import (
    effective_org_id,
    effective_workspace_id,
    reset_request_principal,
    set_request_identity,
)
from tests.support.runtime import LocalPlatformRuntime


def test_worker_claims_and_runs_job_using_the_jobs_tenant_scope():
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
        queued, replayed = runtime.enqueue_agent_run(
            "Summarize the available workspace guide.",
            agent_id=agent.id,
        )
    finally:
        reset_request_principal(identity)

    assert not replayed
    completed = runtime.process_next_execution_job("tenant-worker")

    assert completed is not None
    assert completed.job_id == queued.job_id
    assert completed.status is ExecutionJobStatus.SUCCEEDED
    assert completed.org_id == organization.id
    assert completed.workspace_id == workspace.id
    assert effective_org_id() == "local-org"
    assert effective_workspace_id() == "local-workspace"


def test_local_knowledge_search_uses_its_installation_tenant_scope():
    runtime = LocalPlatformRuntime()
    organization, workspace, _, _, _ = runtime.create_organization(
        name="Knowledge Tenant",
        email="owner@knowledge.example",
        display_name="Knowledge Owner",
    )
    installation = runtime.installations.list_scoped(organization.id, workspace.id)[0]
    result = runtime.context.invoke_for_installation(
        "knowledge.search",
        {"query": "getting started"},
        installation,
    )

    assert [item["source_id"] for item in result["documents"]] == [
        f"onboarding:{organization.id}"
    ]
