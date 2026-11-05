"""Shared base for runtime capability mixins composed by the application."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import Event
from typing import TYPE_CHECKING, Any

from application.ports.repository import Repository
from clients.integrations.mcp import McpHttpPlugin
from clients.model_providers.gateway import ModelGateway
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    ApprovalPolicy,
    ApprovalRequest,
    AuditEvent,
    ErrorSummary,
    EvaluationExecution,
    EvaluationReport,
    ExecutionJob,
    ExecutionJobStatus,
    ModelProfile,
    OrganizationModelConfiguration,
    PluginInstallation,
    ToolInvocation,
    WorkflowRun,
)
from modules.workflows.runtime_result import RuntimeResult

if TYPE_CHECKING:
    from modules.agents.bootstrap import AgentBootstrapService
    from modules.agents.execution_service import AgentExecutionService
    from modules.auth.authentication_service import AuthenticationService
    from modules.auth.bootstrap import IdentityBootstrapService
    from modules.auth.credential_service import CredentialService
    from modules.auth.organization_service import OrganizationService
    from modules.auth.user_administration_service import UserAdministrationService
    from modules.auth.workspace_access_service import WorkspaceAccessService
    from modules.evaluations.service import EvaluationService
    from modules.executions.recovery_service import ExecutionRecoveryService
    from modules.executions.service import ExecutionService
    from modules.executions.worker_service import ExecutionWorkerService
    from modules.governance.action_recovery_service import ActionRecoveryService
    from modules.governance.service import GovernanceService
    from modules.integrations.admin_service import IntegrationAdminService
    from modules.integrations.bootstrap import IntegrationBootstrapService
    from modules.integrations.connection_service import IntegrationConnectionService
    from modules.knowledge.bootstrap import KnowledgeBootstrapService
    from modules.knowledge.runtime import KnowledgeService
    from modules.memory.runtime import MemoryService
    from modules.models.bootstrap import ModelBootstrapService
    from modules.models.catalog import ModelCatalogService
    from modules.models.runtime import ModelService
    from modules.tasks.service import TaskService
    from modules.workflows.automation_service import WorkflowAutomationService
    from modules.workflows.definition_service import WorkflowDefinitionService
    from modules.workflows.execution_service import WorkflowExecutionService


class RuntimeCapabilityHost:
    """Composition boundary for capability use cases on the local runtime."""

    _bootstrapped: bool
    evaluation_service: EvaluationService
    recovery_service: ExecutionRecoveryService
    action_recovery_service: ActionRecoveryService
    knowledge: KnowledgeService
    memory_service: MemoryService
    model_service: ModelService
    model_catalog_service: ModelCatalogService
    authentication_service: AuthenticationService
    agent_execution_service: AgentExecutionService
    credential_service: CredentialService
    user_administration_service: UserAdministrationService
    workspace_access_service: WorkspaceAccessService
    organization_service: OrganizationService
    execution_service: ExecutionService
    execution_worker_service: ExecutionWorkerService
    governance_service: GovernanceService
    integration_admin_service: IntegrationAdminService
    integration_connection_service: IntegrationConnectionService
    workflow_automation_service: WorkflowAutomationService
    workflow_definition_service: WorkflowDefinitionService
    workflow_execution_service: WorkflowExecutionService
    task_service: TaskService
    agent_bootstrap_service: AgentBootstrapService
    integration_bootstrap_service: IntegrationBootstrapService
    identity_bootstrap_service: IdentityBootstrapService
    model_bootstrap_service: ModelBootstrapService
    knowledge_bootstrap_service: KnowledgeBootstrapService
    _advisory_transaction_lock: Callable[[str], None] | None
    MAX_AGENT_CONTEXT_RECORDS: int
    MAX_AGENT_CONTEXT_SERIALIZED_CHARS: int
    MAX_WORKFLOW_REFERENCE_SERIALIZED_CHARS: int
    execution_jobs: Repository[ExecutionJob]
    runs: Repository[WorkflowRun]
    audit_events: Repository[AuditEvent]
    evaluation_executions: Repository[EvaluationExecution]
    model_profiles: Repository[ModelProfile]
    model_configurations: Repository[OrganizationModelConfiguration]
    agents: Repository[AgentDefinition]
    agent_runs: Repository[AgentRun]
    agent_versions: Repository[AgentVersion]
    approval_policies: Repository[ApprovalPolicy]
    approvals: Repository[ApprovalRequest]
    tool_invocations: Repository[ToolInvocation]
    mcp_plugin: McpHttpPlugin
    model_gateway: ModelGateway

    def bootstrap(self) -> None:
        """Initialize the runtime's built-in product records."""
        raise NotImplementedError

    def _ensure_bootstrapped(self) -> None:
        """Initialize built-in records when a use case requires them."""
        raise NotImplementedError

    def is_bootstrapped(self) -> bool:
        """Return whether application bootstrap has completed."""
        return self._bootstrapped

    def authorize(self, principal_id: str, permission: Any) -> None:
        """Enforce a permission for the active request principal."""
        raise NotImplementedError

    def _create_external_context_installation(
        self,
        *,
        plugin_id: str,
        display_name: str,
        credential: str,
        configuration: dict[str, Any],
        capability_ids: tuple[str, ...],
        audit_metadata: dict[str, Any],
        requested_by_principal_id: str,
    ) -> PluginInstallation:
        """Persist external connector credentials through the connection service."""
        raise NotImplementedError

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction over the configured persistence adapter."""
        raise NotImplementedError

    def _complete_claimed_execution_job(
        self, job: ExecutionJob, *, cancellation_requested: Event | None = None
    ) -> ExecutionJob:
        """Run and persist the terminal state for a claimed job."""
        raise NotImplementedError

    def _execute_claimed_job(
        self, cancellation_requested: Event, job: ExecutionJob
    ) -> tuple[
        str,
        ErrorSummary | None,
        EvaluationReport | None,
        str | None,
        ExecutionJobStatus,
        str,
    ]:
        """Execute a job payload and return its normalized terminal outcome."""
        raise NotImplementedError

    def _execution_cancellation_requested(
        self, job_id: str, cancellation_requested: Event
    ) -> bool:
        """Refresh cancellation from the durable execution job record."""
        raise NotImplementedError

    def run_workflow(
        self,
        workflow_id: str,
        task: str,
        requested_by_principal_id: str = "local-developer",
        thread_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        pinned_workflow_version_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Execute a published workflow synchronously."""
        raise NotImplementedError

    def run_agent(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        agent_version_id: str | None = None,
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Execute one agent run synchronously."""
        raise NotImplementedError

    def _execute_agent_run(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        workflow_id: str | None = None,
        agent_version_id: str | None = None,
        pinned_workflow_version_id: str | None = None,
        workflow_run: WorkflowRun | None = None,
        finalize_run: bool = True,
        workflow_step_index: int = 0,
        workflow_step_count: int = 1,
        previous_step_artifacts: tuple[Any, ...] = (),
        _cancellation_event: Event | None = None,
        _cancellation_check: Callable[[], bool] | None = None,
    ) -> RuntimeResult:
        """Execute one agent step inside a workflow run context."""
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        """Resolve collaborators attached by the runtime composition root."""
        raise AttributeError(name)
