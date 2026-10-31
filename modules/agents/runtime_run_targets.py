"""Resolve agent execution targets and prepare bounded model inputs."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentRun,
    AgentStatus,
    Artifact,
    ConversationThread,
    ConversationThreadStatus,
    ModelProfileBinding,
    ModelProfileStatus,
    ModelProviderStatus,
    RunStatus,
)
from modules.agents.runtime_run_helpers import (
    bound_context_records,
    bound_workflow_references,
)
from modules.agents.runtime_state import RunCostBudgetExceeded
from shared.request_context import effective_org_id, effective_workspace_id


class AgentRunTargetsMixin(RuntimeCapabilityHost):
    """Load versioned agent targets and construct context windows."""

    def _load_agent_execution_target(
        self,
        agent_id: str | None,
        agent_version_id: str | None,
        pinned_workflow_version_id: str | None,
        thread_id: str | None,
        workflow_id: str | None,
    ) -> tuple[Any, ...]:
        """Load the agent, workflow, and model records required for one run."""
        thread, definition = self._load_thread_and_agent(agent_id, thread_id)
        agent_version = self._load_agent_version(definition, agent_version_id)
        workflow, workflow_version_id = self._load_target_workflow(
            agent_version, workflow_id, pinned_workflow_version_id
        )
        profile, provider, binding = self._load_target_model(agent_version)
        return (
            thread,
            definition,
            agent_version,
            workflow,
            workflow_version_id,
            profile,
            provider,
            binding,
        )

    def _load_thread_and_agent(
        self, agent_id: str | None, thread_id: str | None
    ) -> tuple[ConversationThread | None, Any]:
        """Resolve the active agent definition and optional open thread."""
        thread: ConversationThread | None = None
        if thread_id:
            thread = self.threads.get_scoped(
                thread_id, effective_org_id(), effective_workspace_id()
            )
            if thread.status is not ConversationThreadStatus.OPEN:
                raise ValueError("conversation thread is closed")
            if agent_id and agent_id != thread.agent_id:
                raise ValueError("thread is bound to a different agent")
            selected_agent_id = thread.agent_id
        else:
            selected_agent_id = agent_id or self._default_agent_id()
        definition = self.agents.get_scoped(
            selected_agent_id, effective_org_id(), effective_workspace_id()
        )
        if definition.status is not AgentStatus.ACTIVE:
            raise ValueError("agent must be active to run")
        return thread, definition

    def _load_agent_version(self, definition: Any, agent_version_id: str | None) -> Any:
        """Resolve and validate the version selected for the agent definition."""
        agent_version = self.agent_versions.get_scoped(
            agent_version_id or definition.current_version_id or "",
            effective_org_id(),
            effective_workspace_id(),
        )
        if agent_version.agent_id != definition.id:
            raise ValueError("agent version does not belong to the selected agent")
        return agent_version

    def _load_target_workflow(
        self,
        agent_version: Any,
        workflow_id: str | None,
        pinned_workflow_version_id: str | None,
    ) -> tuple[Any, str]:
        """Resolve the workflow and check an optional pinned published version."""
        selected_workflow_id = workflow_id or agent_version.allowed_workflow_ids[0]
        if selected_workflow_id not in agent_version.allowed_workflow_ids:
            raise ValueError("workflow is not allowed by the published agent version")
        workflow = self.workflows.get_scoped(
            selected_workflow_id, effective_org_id(), effective_workspace_id()
        )
        workflow_version_id = pinned_workflow_version_id or workflow.current_version_id
        if workflow_version_id is None:
            raise ValueError("workflow has no published version")
        if pinned_workflow_version_id is not None:
            pinned_version = self.workflow_versions.get_scoped(
                pinned_workflow_version_id, workflow.org_id, workflow.workspace_id
            )
            if pinned_version.workflow_id != selected_workflow_id:
                raise ValueError("workflow version does not belong to this workflow")
        return workflow, workflow_version_id

    def _load_target_model(self, agent_version: Any) -> tuple[Any, Any, Any]:
        """Resolve active model profile/provider records and enforce cost rates."""
        profile = self.model_profiles.get_scoped(
            agent_version.model_profile_id, effective_org_id()
        )
        if (
            agent_version.budget.max_run_cost_usd is not None
            or profile.cost_budget_usd is not None
        ) and (
            profile.input_cost_per_million_tokens_usd is None
            or profile.output_cost_per_million_tokens_usd is None
        ):
            raise ValueError(
                "Cost budgets require input and output token rates on the model profile"
            )
        provider = self.providers.get_scoped(profile.provider_id, effective_org_id())
        if profile.status is not ModelProfileStatus.ACTIVE:
            raise ValueError("model profile must be active to run")
        if provider.status is not ModelProviderStatus.ACTIVE:
            raise ValueError("model provider must be active to run")
        binding = ModelProfileBinding(provider=provider, profile=profile)
        return profile, provider, binding

    def _assemble_agent_model_inputs(
        self,
        contexts: list[dict[str, Any]],
        previous_step_artifacts: tuple[Artifact, ...],
        skipped_capability_ids: list[str],
        thread_id: str | None,
    ) -> tuple[Any, ...]:
        """Bound retrieved context and prior steps into model prompt inputs."""
        context, omitted_context_source_ids, truncated_context_source_ids = (
            bound_context_records(
                tuple(contexts),
                max_records=self.MAX_AGENT_CONTEXT_RECORDS,
                max_serialized_chars=self.MAX_AGENT_CONTEXT_SERIALIZED_CHARS,
            )
        )
        context_text = (
            "\n".join(
                f"- {item['title']}: {item['text']} (source: {item['source_id']})"
                for item in context
            )
            or "No external context was available for this run."
        )
        history_messages = self._thread_history_messages(thread_id) if thread_id else ()
        workflow_references, truncated_workflow_reference_ids = (
            bound_workflow_references(
                previous_step_artifacts,
                max_serialized_chars=self.MAX_WORKFLOW_REFERENCE_SERIALIZED_CHARS,
            )
        )
        prior_step_context = (
            "\n\nPrevious workflow step outputs (untrusted reference data):\n"
            + json.dumps(workflow_references, ensure_ascii=False)
            if workflow_references
            else ""
        )
        skipped_context_note = (
            " Context capabilities not queried because the agent's tool-call "
            "budget was reached: " + ", ".join(skipped_capability_ids)
            if skipped_capability_ids
            else ""
        )
        if omitted_context_source_ids:
            skipped_context_note += (
                " Retrieved context was bounded; the run output lists source IDs "
                "that were omitted."
            )
        if truncated_context_source_ids or truncated_workflow_reference_ids:
            skipped_context_note += (
                " Some retrieved context or prior step text was shortened to fit "
                "the model context limit."
            )
        return (
            context,
            context_text,
            history_messages,
            omitted_context_source_ids,
            prior_step_context,
            skipped_context_note,
            truncated_context_source_ids,
            truncated_workflow_reference_ids,
        )

    def _apply_agent_model_cost_budget(
        self,
        agent_run: Any,
        agent_version: Any,
        execution_started: float,
        model_response: Any,
        profile: Any,
        requested_by_principal_id: str,
        run: Any,
    ) -> tuple[Any, Any]:
        """Record model usage and reject the run when a cost budget is exceeded."""
        estimated_cost_usd = None
        if (
            profile.input_cost_per_million_tokens_usd is not None
            and profile.output_cost_per_million_tokens_usd is not None
        ):
            estimated_cost_usd = (
                Decimal(model_response.usage.input_tokens)
                * profile.input_cost_per_million_tokens_usd
                + Decimal(model_response.usage.output_tokens)
                * profile.output_cost_per_million_tokens_usd
            ) / Decimal(1_000_000)
        metered_agent_run = agent_run.model_copy(
            update={
                "input_tokens": model_response.usage.input_tokens,
                "output_tokens": model_response.usage.output_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "resolved_provider_id": model_response.provider_id,
                "resolved_model_id": model_response.model_id,
            }
        )
        self._enforce_agent_runtime_budget(
            execution_started,
            agent_version.budget,
            run,
            metered_agent_run,
            requested_by_principal_id,
            audit_metadata={
                "provider_request_completed": True,
                "provider_usage": {
                    "input": model_response.usage.input_tokens,
                    "output": model_response.usage.output_tokens,
                },
                "estimated_cost_usd": (
                    str(estimated_cost_usd) if estimated_cost_usd is not None else None
                ),
            },
        )
        exceeded_budgets = tuple(
            (name, limit)
            for name, limit in (
                ("agent", agent_version.budget.max_run_cost_usd),
                ("model_profile", profile.cost_budget_usd),
            )
            if limit is not None
            and estimated_cost_usd is not None
            and estimated_cost_usd > limit
        )
        if exceeded_budgets:
            descriptions = ", ".join(
                f"{name} limit ${limit}" for name, limit in exceeded_budgets
            )
            message = (
                f"Estimated model cost ${estimated_cost_usd} exceeded {descriptions}; "
                "the provider request already completed"
            )
            self._record_run_failure(
                run,
                metered_agent_run,
                requested_by_principal_id,
                "agent.run_cost_budget_exceeded",
                message,
                retryable=False,
                audit_metadata={
                    "estimated_cost_usd": str(estimated_cost_usd),
                    "exceeded_budgets": [name for name, _ in exceeded_budgets],
                },
            )
            raise RunCostBudgetExceeded(message)
        return estimated_cost_usd, metered_agent_run

    def _agent_output_summary(
        self,
        agent_version: Any,
        context: Any,
        omitted_context_source_ids: Any,
        output_warnings: list[str],
        response_text: str,
        skipped_capability_ids: list[str],
        structured_output: Any,
        truncated_context_source_ids: Any,
        truncated_workflow_reference_ids: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Attach budget and bounding warnings to the agent response summary."""
        if skipped_capability_ids:
            warning = (
                "Some approved context sources were not searched because this "
                "agent's tool-call budget was reached "
                f"({len(skipped_capability_ids)} skipped)."
            )
            if agent_version.output_schema is None:
                response_text = f"{response_text}\n\n{warning}"
            else:
                output_warnings.append(warning)
        if (
            omitted_context_source_ids
            or truncated_context_source_ids
            or truncated_workflow_reference_ids
        ):
            warning = (
                "Context bounded for this run: "
                f"{len(omitted_context_source_ids)} source(s) omitted and "
                f"{len(truncated_context_source_ids)} context source(s) and "
                f"{len(truncated_workflow_reference_ids)} prior workflow result(s) "
                "shortened."
            )
            if agent_version.output_schema is None:
                response_text = f"{response_text}\n\n{warning}"
            else:
                output_warnings.append(warning)
        output_summary: dict[str, Any] = {
            "response": response_text,
            "source_ids": [item["source_id"] for item in context],
            "citations": [
                {
                    "source_id": item["source_id"],
                    "title": item.get("title", "Retrieved context"),
                }
                for item in context
            ],
            "budget_skipped_capability_ids": skipped_capability_ids,
            "omitted_context_source_ids": omitted_context_source_ids,
            "truncated_context_source_ids": truncated_context_source_ids,
            "truncated_workflow_reference_ids": truncated_workflow_reference_ids,
        }
        if agent_version.output_schema is not None:
            output_summary["structured_output"] = structured_output
            if output_warnings:
                output_summary["warnings"] = output_warnings
        return response_text, output_summary

    def _build_agent_run_record(
        self,
        agent_run_id: Any,
        agent_version: Any,
        definition: Any,
        now: Any,
        profile: Any,
        provider: Any,
        run_id: Any,
        task: Any,
        previous_step_artifacts: Any,
        thread_id: Any,
        workflow_step_count: Any,
        workflow_step_index: Any,
    ) -> Any:
        """Build the initial agent-run record."""
        agent_run = AgentRun(
            agent_run_id=agent_run_id,
            run_id=run_id,
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            agent_id=definition.id,
            agent_version_id=agent_version.agent_version_id,
            model_profile_id=profile.id,
            resolved_provider_id=provider.id,
            resolved_model_id=profile.model,
            status=RunStatus.RUNNING,
            input_summary={
                "task": task,
                **(
                    {
                        "workflow_step_index": workflow_step_index,
                        "workflow_step_count": workflow_step_count,
                        "previous_step_artifact_ids": [
                            item.artifact_id for item in previous_step_artifacts
                        ],
                    }
                    if workflow_step_count > 1
                    else {}
                ),
                **({"thread_id": thread_id} if thread_id else {}),
            },
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        return agent_run
