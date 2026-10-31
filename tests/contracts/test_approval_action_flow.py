from datetime import UTC, datetime, timedelta

import httpx
import pytest

from contracts.v1 import (
    ApprovalRequest,
    ApprovalStatus,
    InvocationStatus,
    RunStatus,
)
from infrastructure.integrations.plugin_gateway import PluginGatewayError
from tests.support.runtime import LocalPlatformRuntime


def _connected_jira_runtime(responder):
    runtime = LocalPlatformRuntime()
    client = httpx.Client(transport=httpx.MockTransport(responder))
    runtime.jira_plugin._client = client
    runtime.bootstrap()
    installation = runtime.create_jira_installation(
        display_name="Weaves Jira",
        api_token="jira-test-token",
        site_url="https://example.atlassian.net",
        email="dev@example.com",
        project_keys=("WEAVES",),
    )
    runtime.integration_admin_service.update_plugin_installation(
        installation.plugin_installation_id,
        enabled_capability_ids=("jira.issues.search", "jira.issues.comment"),
    )
    agent = runtime.agent_service.create_agent(
        name="Jira assistant",
        instructions="Use context to suggest next steps.",
        allowed_capability_ids=("knowledge.search", "jira.issues.comment"),
    )
    return runtime, client, installation, agent


def _successful_run(runtime, agent_id):
    result = runtime.run_agent("Summarize the release checklist.", agent_id=agent_id)
    assert result.run.status is RunStatus.SUCCEEDED
    return result.run.run_id


def test_jira_comment_waits_for_approval_and_gateway_requires_approval():
    writes = []

    def respond(request):
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/comment"):
            writes.append(request)
            return httpx.Response(201, json={"id": "comment-42"})
        raise AssertionError(f"unexpected Jira request: {request.url}")

    runtime, client, installation, agent = _connected_jira_runtime(respond)
    try:
        runtime.bootstrap()
        run_id = _successful_run(runtime, agent.id)
        approval, proposed = runtime.governance_service.propose_jira_comment(
            run_id,
            issue_key="WEAVES-17",
            comment="The release checklist is ready.",
        )

        assert proposed.status is InvocationStatus.AWAITING_APPROVAL
        assert writes == []

        agent_version = runtime.agent_versions.get_scoped(
            agent.current_version_id, "local-org", "local-workspace"
        )
        with pytest.raises(PluginGatewayError, match="approved action request"):
            runtime.plugin_gateway.invoke(
                org_id="local-org",
                workspace_id="local-workspace",
                installation_id=installation.plugin_installation_id,
                agent_version=agent_version,
                capability_id="jira.issues.comment",
                payload={
                    "issue_key": "WEAVES-17",
                    "comment": "Direct unapproved write",
                },
            )
        assert writes == []

        resolved, invocation = runtime.governance_service.decide_approval(
            approval.approval_request_id, approved=True
        )

        assert resolved.status.value == "approved"
        assert invocation.status is InvocationStatus.SUCCEEDED
        assert invocation.output_summary == {
            "comment_id": "comment-42",
            "issue_url": "https://example.atlassian.net/browse/WEAVES-17",
        }
        assert len(writes) == 1
    finally:
        client.close()
        runtime.close()


def test_rejected_jira_comment_is_never_sent():
    writes = []

    def respond(request):
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/comment"):
            writes.append(request)
            return httpx.Response(201, json={"id": "unexpected"})
        raise AssertionError(f"unexpected Jira request: {request.url}")

    runtime, client, _, agent = _connected_jira_runtime(respond)
    try:
        run_id = _successful_run(runtime, agent.id)
        approval, _ = runtime.governance_service.propose_jira_comment(
            run_id,
            issue_key="WEAVES-17",
            comment="This should not be posted.",
        )
        resolved, invocation = runtime.governance_service.decide_approval(
            approval.approval_request_id,
            approved=False,
            decision_comment="Needs more context",
        )

        assert resolved.status.value == "rejected"
        assert invocation.status is InvocationStatus.DENIED
        assert writes == []
    finally:
        client.close()
        runtime.close()


def test_losing_approval_decision_cannot_dispatch_connector_write(monkeypatch):
    writes = []

    def respond(request):
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/comment"):
            writes.append(request)
            return httpx.Response(201, json={"id": "comment-race"})
        raise AssertionError(f"unexpected Jira request: {request.url}")

    runtime, client, _, agent = _connected_jira_runtime(respond)
    try:
        run_id = _successful_run(runtime, agent.id)
        approval, invocation = runtime.governance_service.propose_jira_comment(
            run_id,
            issue_key="WEAVES-17",
            comment="This action can be dispatched only once.",
        )

        def lose_compare_and_set(*args, **kwargs):
            return False

        monkeypatch.setattr(
            runtime.approvals, "put_if_status_and_stale", lose_compare_and_set
        )
        with pytest.raises(ValueError, match="already resolved"):
            runtime.governance_service.decide_approval(
                approval.approval_request_id, approved=True
            )

        assert (
            runtime.approvals.get(approval.approval_request_id).status.value
            == "pending"
        )
        assert runtime.tool_invocations.get(invocation.invocation_id).status is (
            InvocationStatus.AWAITING_APPROVAL
        )
        assert writes == []
    finally:
        client.close()
        runtime.close()


def test_recovery_marks_interrupted_approved_write_as_unknown_without_retry():
    writes = []

    def respond(request):
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/comment"):
            writes.append(request)
            return httpx.Response(201, json={"id": "unexpected-recovery-write"})
        raise AssertionError(f"unexpected Jira request: {request.url}")

    runtime, client, _, agent = _connected_jira_runtime(respond)
    try:
        run_id = _successful_run(runtime, agent.id)
        approval, invocation = runtime.governance_service.propose_jira_comment(
            run_id,
            issue_key="WEAVES-17",
            comment="The result must be checked after interruption.",
        )
        stale_at = datetime.now(UTC) - timedelta(minutes=5)
        approved = ApprovalRequest.model_validate(
            approval.model_copy(
                update={
                    "created_at": stale_at,
                    "requested_at": stale_at,
                    "status": ApprovalStatus.APPROVED,
                    "resolved_by_principal_id": "local-developer",
                    "resolved_at": stale_at + timedelta(seconds=1),
                    "updated_at": stale_at + timedelta(seconds=1),
                }
            ).model_dump()
        )
        executing = invocation.model_copy(
            update={
                "status": InvocationStatus.EXECUTING,
                "started_at": stale_at + timedelta(seconds=2),
                "updated_at": stale_at + timedelta(seconds=2),
            }
        )
        runtime.approvals.put(approved)
        runtime.tool_invocations.put(
            type(invocation).model_validate(executing.model_dump())
        )

        recovered = runtime.recover_stale_action_invocations(
            now=datetime.now(UTC), stale_after_seconds=60
        )

        assert recovered == (invocation.invocation_id,)
        timed_out = runtime.tool_invocations.get(invocation.invocation_id)
        assert timed_out.status is InvocationStatus.TIMED_OUT
        assert timed_out.error is not None
        assert timed_out.error.code == "action.interrupted"
        assert timed_out.error.retryable is False
        assert "outcome is unknown" in timed_out.error.summary
        assert runtime.recover_stale_action_invocations() == ()
        assert writes == []
        assert (
            sum(
                event.action == "action.execution_interrupted"
                and event.target_id == invocation.invocation_id
                for event in runtime.audit_events.list()
            )
            == 1
        )
    finally:
        client.close()
        runtime.close()


def test_multi_agent_workflow_requires_selecting_write_capable_step():
    def respond(request):
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/comment"):
            return httpx.Response(201, json={"id": "comment-sequence"})
        raise AssertionError(f"unexpected Jira request: {request.url}")

    runtime, client, _, first_agent = _connected_jira_runtime(respond)
    try:
        second_agent = runtime.agent_service.create_agent(
            name="Jira reviewer",
            instructions="Review the plan and identify risks.",
            allowed_capability_ids=("knowledge.search", "jira.issues.comment"),
        )
        workflow = runtime.create_workflow(
            name="Jira review sequence",
            agent_ids=(first_agent.id, second_agent.id),
        )
        result = runtime.run_workflow(
            workflow.workflow_id, "Review the release checklist"
        )

        with pytest.raises(ValueError, match="agent_run_id is required"):
            runtime.governance_service.propose_jira_comment(
                result.run.run_id,
                issue_key="WEAVES-17",
                comment="The release checklist is ready.",
            )

        approval, invocation = runtime.governance_service.propose_jira_comment(
            result.run.run_id,
            issue_key="WEAVES-17",
            comment="The release checklist is ready.",
            agent_run_id=result.workflow_agent_runs[0].agent_run_id,
        )
        assert (
            approval.requested_by_agent_run_id
            == result.workflow_agent_runs[0].agent_run_id
        )
        assert invocation.status is InvocationStatus.AWAITING_APPROVAL
    finally:
        client.close()
        runtime.close()
