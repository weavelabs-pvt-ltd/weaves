"""Seed the built-in assistant and workflow for a new local installation."""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from application.ports.repository import Repository
from contracts.v1 import (
    AgentDefinition,
    AgentStatus,
    AgentVersion,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowVersion,
)
from contracts.v1.base import ProductContract

TProduct = TypeVar("TProduct", bound=ProductContract)


class AgentBootstrapService:
    """Create the starter assistant and its initial workflow records."""

    def __init__(
        self,
        *,
        agents: Repository[AgentDefinition],
        agent_versions: Repository[AgentVersion],
        workflows: Repository[WorkflowDefinition],
        workflow_versions: Repository[WorkflowVersion],
        default_model_profile_id: str,
        provider_backed: bool,
    ) -> None:
        """Bind agent repositories and configured starter profile details."""
        self.agents = agents
        self.agent_versions = agent_versions
        self.workflows = workflows
        self.workflow_versions = workflow_versions
        self.default_model_profile_id = default_model_profile_id
        self.provider_backed = provider_backed

    @staticmethod
    def _seed(
        repository: Repository[TProduct], id_field: str, record: TProduct
    ) -> TProduct:
        """Create a deterministic starter record only when it is absent."""
        record_id = str(getattr(record, id_field))
        try:
            return repository.get(record_id)
        except KeyError:
            try:
                return repository.create(record)
            except ValueError:
                return repository.get(record_id)

    def seed_local_assistant(self, now: datetime) -> None:
        """Seed the local assistant agent and its workflow."""
        version = AgentVersion(
            agent_version_id="local-assistant-v1",
            agent_id="local-assistant",
            org_id="local-org",
            workspace_id="local-workspace",
            version=1,
            persona="A concise, helpful work assistant.",
            instructions=(
                "Use only the supplied approved context. "
                "State when context is insufficient."
            ),
            model_profile_id=self.default_model_profile_id,
            approval_policy_id="local-approval-policy",
            allowed_capability_ids=("knowledge.search",),
            allowed_workflow_ids=("local-assistant-workflow",),
            created_at=now,
        )
        self._seed(self.agent_versions, "agent_version_id", version)
        self._seed(
            self.agents,
            "id",
            AgentDefinition(
                id="local-assistant",
                org_id="local-org",
                workspace_id="local-workspace",
                name="Local assistant",
                description=(
                    "A provider-backed platform runtime example."
                    if self.provider_backed
                    else "A deterministic platform runtime example."
                ),
                current_version_id=version.agent_version_id,
                status=AgentStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
        workflow_version = WorkflowVersion(
            workflow_version_id="local-assistant-workflow-v1",
            workflow_id="local-assistant-workflow",
            org_id="local-org",
            workspace_id="local-workspace",
            version=1,
            handler_key="agent.execute",
            agent_version_ids=(version.agent_version_id,),
            required_capability_ids=("knowledge.search",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            created_at=now,
        )
        self._seed(self.workflow_versions, "workflow_version_id", workflow_version)
        self._seed(
            self.workflows,
            "workflow_id",
            WorkflowDefinition(
                workflow_id="local-assistant-workflow",
                org_id="local-org",
                workspace_id="local-workspace",
                name="Run local assistant",
                current_version_id=workflow_version.workflow_version_id,
                status=WorkflowStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
