import json

import pytest

from application.platform import (
    AgentRuntimeBudgetExceeded,
)
from clients.model_providers.gateway import ModelGateway, ModelGatewayError
from contracts.v1 import (
    AgentBudget,
    AgentVersion,
    MemoryPolicy,
    MemoryScope,
    ModelProviderType,
    ModelResponse,
    ModelUsage,
    RunStatus,
    WorkflowStepCondition,
)
from tests.support.runtime import LocalPlatformRuntime


def test_conversation_thread_sends_bounded_prior_turns_to_model():
    runtime = LocalPlatformRuntime()
    captured_requests = []

    class CapturingModel:
        def complete(self, request, binding):
            captured_requests.append(request)
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content="Acknowledged.",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

    runtime.bootstrap()
    runtime.model_adapters[ModelProviderType.LOCAL] = CapturingModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)
    thread = runtime.memory_service.create_conversation_thread(title="Follow up")

    runtime.run_agent("My team is called Atlas", thread_id=thread.thread_id)
    runtime.run_agent("What is my team called?", thread_id=thread.thread_id)

    messages = captured_requests[-1].messages
    assert messages[1].role.value == "user"
    assert messages[1].content == "My team is called Atlas"
    assert messages[2].role.value == "assistant"
    assert messages[2].content.startswith("Acknowledged.")
    assert messages[-1].content.startswith("Task:\nWhat is my team called?")


def test_agent_run_persists_responses_longer_than_old_artifact_summary_limit():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    response_text = "Detailed result. " * 1500

    class LongResponseModel:
        def complete(self, request, binding):
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content=response_text,
                usage=ModelUsage(input_tokens=1, output_tokens=2000, total_tokens=2001),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = LongResponseModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)

    result = runtime.run_agent("Return a detailed working agreement")

    assert len(result.artifact.summary) > 20_000
    assert result.agent_run.output_summary is not None
    assert len(result.agent_run.output_summary["response"]) > 20_000


def test_agent_context_is_bounded_to_model_and_artifact_contracts():
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="Large context agent",
        instructions="Use the supplied release context.",
    )
    version = runtime.agent_versions.get(agent.current_version_id or "")
    runtime.agent_versions.put(
        AgentVersion.model_validate(
            version.model_copy(
                update={
                    "memory_policy": MemoryPolicy(
                        allowed_scopes=(MemoryScope.WORKSPACE,), max_items=50
                    )
                }
            ).model_dump()
        )
    )
    source_ref_prefix = "external-memory-source-"
    for index in range(50):
        runtime.memory_service.create_memory_item(
            title=f"Release guidance {index}",
            text="release deployment guidance " * 350,
            source_ref=f"{source_ref_prefix}{index}-" + "x" * 180,
        )

    captured = []

    class CapturingModel:
        def complete(self, request, binding):
            captured.append(request)
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content="Bounded context response.",
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = CapturingModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)

    result = runtime.run_agent("Summarize release guidance", agent_id=agent.id)

    user_message = captured[0].messages[-1].content
    context_json = user_message.split("Retrieved context records (JSON):\n", 1)[1]
    context_records = json.loads(context_json)
    assert len(user_message) < 100_000
    assert len(context_records) == runtime.MAX_AGENT_CONTEXT_RECORDS
    assert len(result.artifact.provenance) <= 50
    assert len(result.agent_run.output_summary["omitted_context_source_ids"]) > 0
    assert any(
        item.source_ref.startswith(source_ref_prefix)
        for item in result.artifact.provenance
    )


def test_ten_step_workflow_bounds_prior_artifacts_in_model_requests():
    runtime = LocalPlatformRuntime()
    agents = tuple(
        runtime.agent_service.create_agent(
            name=f"Workflow step {index}",
            instructions="Review the previous engineering result.",
        )
        for index in range(10)
    )
    workflow = runtime.create_workflow(
        name="Long-result workflow", agent_ids=tuple(item.id for item in agents)
    )
    captured = []

    class LongWorkflowModel:
        def complete(self, request, binding):
            captured.append(request)
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content="step result " * 2000,
                usage=ModelUsage(input_tokens=1, output_tokens=2000, total_tokens=2001),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = LongWorkflowModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)

    result = runtime.run_workflow(workflow.workflow_id, "Review the release")

    assert len(captured) == 10
    assert len(captured[-1].messages[-1].content) < 100_000
    assert len(result.artifact.provenance) <= 50


def test_workflow_condition_routes_steps_from_structured_agent_output(
    route, operator, expected_value, expected_agents
):
    runtime = LocalPlatformRuntime()
    schema = {
        "type": "object",
        "properties": {"route": {"type": "string"}},
        "required": ["route"],
        "additionalProperties": False,
    }
    router = runtime.agent_service.create_agent(
        name="Router",
        instructions="Classify the request and return a route.",
        allowed_capability_ids=(),
        output_schema=schema,
    )
    reviewer = runtime.agent_service.create_agent(
        name="Reviewer",
        instructions="Review the request.",
        allowed_capability_ids=(),
    )
    summarizer = runtime.agent_service.create_agent(
        name="Summarizer",
        instructions="Summarize the request.",
        allowed_capability_ids=(),
    )
    workflow = runtime.create_workflow(
        name="Conditional review",
        agent_ids=(router.id, reviewer.id, summarizer.id),
    )
    condition = WorkflowStepCondition(
        source_agent_id=router.id,
        target_agent_id=reviewer.id,
        json_pointer="/route",
        operator=operator,
        expected_value=expected_value,
    )
    workflow = runtime.replace_workflow_agents(
        workflow.workflow_id,
        (router.id, reviewer.id, summarizer.id),
        (condition,),
    )
    runtime.agent_service.update_agent(
        router.id, instructions="Route the request carefully."
    )
    workflow = runtime.workflows.get(workflow.workflow_id)
    current_workflow_version = runtime.workflow_versions.get(
        workflow.current_version_id or ""
    )
    assert current_workflow_version.step_conditions == (condition,)

    class ConditionalModel:
        calls = 0

        def complete(self, request, binding):
            self.calls += 1
            content = (
                json.dumps({"route": route})
                if self.calls == 1
                else f"{expected_agents[self.calls - 1]} completed"
            )
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content=content,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = ConditionalModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)
    result = runtime.run_workflow(workflow.workflow_id, "Review this request")

    _assert_conditional_workflow_result(
        runtime,
        workflow.current_version_id,
        (router, reviewer, summarizer),
        expected_agents,
        result,
    )


def _assert_conditional_workflow_result(
    runtime, workflow_version_id, agents, expected_agents, result
):
    """Verify the selected workflow steps and their skip audit record."""

    assert result.run.status is RunStatus.SUCCEEDED
    assert result.run.workflow_version_id == workflow_version_id
    agents_by_name = {agent.name: agent for agent in agents}
    assert tuple(item.agent_id for item in result.workflow_agent_runs) == tuple(
        agents_by_name[name].id for name in expected_agents
    )
    assert [
        item.input_summary["workflow_step_index"] for item in result.workflow_agent_runs
    ] == ([0, 2] if len(expected_agents) == 2 else [0, 1, 2])
    skipped_events = tuple(
        item
        for item in runtime.audit_events.list()
        if item.action == "workflow.step.skipped"
    )
    assert len(skipped_events) == (0 if len(expected_agents) == 3 else 1)


def test_invalid_provider_response_is_sanitized_and_fails_the_run():
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="Malformed provider response",
        instructions="Answer from approved context.",
    )

    class OversizedResponseModel:
        def complete(self, request, binding):
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content="x" * 100_001,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = OversizedResponseModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)

    with pytest.raises(ModelGatewayError) as captured_error:
        runtime.run_agent("Summarize security basics", agent_id=agent.id)

    assert captured_error.value.code == "model.invalid_response"
    assert "100001" not in captured_error.value.message
    failed_run = runtime.runs.list()[-1]
    failed_agent_run = next(
        item for item in runtime.agent_runs.list() if item.run_id == failed_run.run_id
    )
    assert failed_run.status is RunStatus.FAILED
    assert failed_agent_run.status is RunStatus.FAILED


def test_agent_tool_call_budget_skips_excess_context_capabilities():
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="No context calls",
        instructions="Answer from the request alone.",
        allowed_capability_ids=("knowledge.search",),
        budget=AgentBudget(max_tool_calls=0),
    )

    result = runtime.run_agent("Summarize the working agreements", agent_id=agent.id)

    assert result.tool_invocations == ()
    assert result.agent_run.output_summary is not None
    assert result.agent_run.output_summary["budget_skipped_capability_ids"] == [
        "knowledge.search"
    ]
    assert "tool-call budget was reached" in result.artifact.summary


def test_agent_runtime_budget_fails_at_safe_boundary_and_audits(monkeypatch):
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="Short runtime",
        instructions="Answer using connected context.",
        budget=AgentBudget(max_runtime_seconds=1),
    )
    ticks = iter((100.0, 102.0))
    monkeypatch.setattr("application.platform.monotonic", lambda: next(ticks))

    with pytest.raises(AgentRuntimeBudgetExceeded, match="safe boundary"):
        runtime.run_agent("Summarize security basics", agent_id=agent.id)

    failed_run = runtime.runs.list()[-1]
    assert failed_run.status is RunStatus.FAILED
    assert failed_run.error is not None
    assert failed_run.error.code == "agent.runtime_budget_exceeded"
    assert any(
        event.action == "agent.run.failed" and event.target_id == failed_run.run_id
        for event in runtime.audit_events.list()
    )


def test_runtime_timeout_after_model_completion_retains_reported_usage(monkeypatch):
    runtime = LocalPlatformRuntime()
    agent = runtime.agent_service.create_agent(
        name="Provider timeout",
        instructions="Answer using connected context.",
        budget=AgentBudget(max_runtime_seconds=1),
    )

    class SlowModel:
        def complete(self, request, binding):
            return ModelResponse(
                provider_id=binding.provider.id,
                model_id=binding.profile.model,
                content="A completed response.",
                usage=ModelUsage(input_tokens=3, output_tokens=4, total_tokens=7),
            )

    runtime.model_adapters[ModelProviderType.LOCAL] = SlowModel()
    runtime.model_gateway = ModelGateway(runtime.model_adapters)
    ticks = iter((100.0, 100.0, 100.0, 102.0))
    monkeypatch.setattr("application.platform.monotonic", lambda: next(ticks))

    with pytest.raises(AgentRuntimeBudgetExceeded):
        runtime.run_agent("Summarize security basics", agent_id=agent.id)

    failed_run = runtime.runs.list()[-1]
    agent_run = next(
        item for item in runtime.agent_runs.list() if item.run_id == failed_run.run_id
    )
    assert agent_run.input_tokens == 3
    assert agent_run.output_tokens == 4
    assert agent_run.resolved_provider_id == "local-mock-provider"
