"""Typed infrastructure and lifecycle ports for capability composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from threading import RLock
from typing import Protocol

from application.ports.repository import Repository
from application.ports.runtime_adapters import PluginDispatcher
from application.ports.secrets import SecretStore
from application.ports.throttling import LoginRateLimiter
from clients.integrations.github import GitHubReadPlugin
from clients.integrations.jira import JiraReadPlugin
from clients.integrations.mcp import McpHttpPlugin
from clients.integrations.slack import SlackReadPlugin
from clients.model_providers.gateway import ModelAdapter, ModelGateway
from contracts.v1 import (
    AgentDefinition,
    AgentRun,
    AgentVersion,
    ApiCredential,
    ApprovalPolicy,
    ApprovalRequest,
    Artifact,
    AuditEvent,
    ConversationThread,
    EvaluationExecution,
    EvaluationReportRecord,
    EvaluationSuiteRecord,
    ExecutionJob,
    KnowledgeDocument,
    MemoryItem,
    ModelProfile,
    ModelProviderType,
    Organization,
    Permission,
    PluginDescriptor,
    PluginInstallation,
    Principal,
    Role,
    RoleBinding,
    ToolInvocation,
    User,
    UserInvitation,
    UserPasswordCredential,
    UserSession,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTrigger,
    WorkflowVersion,
    Workspace,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.auth.runtime_organizations import MockModelIdentifier
from modules.memory.context import MemoryContextBuilder


class RuntimeCompositionInputs(Protocol):
    """Define only the process ports consumed while building capabilities."""

    _advisory_transaction_lock: Callable[[str], None] | None
    _approval_lock: RLock
    _bootstrapped: bool
    _execution_job_idempotency_locks: dict[tuple[str, str], RLock]
    _idempotency_guard: RLock
    _idempotency_locks: dict[tuple[str, str], RLock]

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the process-configured unit of work."""
        ...

    agent_runs: Repository[AgentRun]
    agent_versions: Repository[AgentVersion]
    agents: Repository[AgentDefinition]
    api_credentials: Repository[ApiCredential]
    approval_policies: Repository[ApprovalPolicy]
    approvals: Repository[ApprovalRequest]
    artifacts: Repository[Artifact]
    audit_events: Repository[AuditEvent]
    evaluation_executions: Repository[EvaluationExecution]
    evaluation_reports: Repository[EvaluationReportRecord]
    evaluation_suites: Repository[EvaluationSuiteRecord]
    execution_jobs: Repository[ExecutionJob]
    installations: Repository[PluginInstallation]
    knowledge_documents: Repository[KnowledgeDocument]
    memory_items: Repository[MemoryItem]
    model_profiles: Repository[ModelProfile]
    organizations: Repository[Organization]
    plugins: Repository[PluginDescriptor]
    principals: Repository[Principal]
    providers: Repository[ModelProviderContract]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    runs: Repository[WorkflowRun]
    threads: Repository[ConversationThread]
    tool_invocations: Repository[ToolInvocation]
    user_invitations: Repository[UserInvitation]
    user_passwords: Repository[UserPasswordCredential]
    user_sessions: Repository[UserSession]
    users: Repository[User]
    workflow_schedules: Repository[WorkflowSchedule]
    workflow_triggers: Repository[WorkflowTrigger]
    workflow_versions: Repository[WorkflowVersion]
    workflows: Repository[WorkflowDefinition]
    workspaces: Repository[Workspace]
    default_model_profile_id: str
    environment_model_base_url: str
    environment_model_enabled: bool
    environment_model_name: str
    environment_model_provider_type: ModelProviderType
    github_plugin: GitHubReadPlugin
    jira_plugin: JiraReadPlugin
    login_rate_limiter: LoginRateLimiter
    mcp_plugin: McpHttpPlugin
    memory_context: MemoryContextBuilder
    model: MockModelIdentifier
    model_adapters: Mapping[ModelProviderType, ModelAdapter]
    model_gateway: ModelGateway
    onboarding_rate_limiter: LoginRateLimiter
    openai_enabled: bool
    plugin_gateway: PluginDispatcher
    secret_store: SecretStore
    slack_plugin: SlackReadPlugin

    def authenticate_principal(self, principal_id: str) -> str:
        """Resolve a principal before authentication services are composed."""
        ...

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Expose the workspace authorization use case to service builders."""
        ...

    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Expose organization authorization to service builders."""
        ...

    def bootstrap(self) -> None:
        """Expose the application bootstrap lifecycle to capability services."""
        ...

    def enqueue_agent_run(
        self,
        task: str,
        requested_by_principal_id: str = "local-developer",
        agent_id: str | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        workflow_id: str | None = None,
        trigger_id: str | None = None,
        trigger_event_id: str | None = None,
        schedule_id: str | None = None,
        schedule_occurrence_at: datetime | None = None,
        _idempotency_lock_held: bool = False,
    ) -> tuple[ExecutionJob, bool]:
        """Expose durable agent enqueueing to workflow automation."""
        ...
