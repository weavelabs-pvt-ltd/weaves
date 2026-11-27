"""Typed infrastructure and lifecycle ports for capability composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from threading import RLock
from typing import Protocol

from application.ports.product_repositories import ProductRepositorySet
from application.ports.repository import Repository
from application.ports.runtime_adapters import PluginDispatcher
from application.ports.secrets import SecretStore
from application.ports.throttling import LoginRateLimiter
from application.ports.usage import UsageLedger
from clients.integrations.github import GitHubReadPlugin
from clients.integrations.jira import JiraReadPlugin
from clients.integrations.mcp import McpHttpPlugin
from clients.integrations.slack import SlackReadPlugin
from clients.model_providers.gateway import ModelAdapter, ModelGateway
from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountOAuthState,
    AccountOrganizationRequest,
    AccountSession,
    AgentDefinition,
    AgentRun,
    AgentVersion,
    ApiCredential,
    Artifact,
    AuditEvent,
    ConversationThread,
    EvaluationExecution,
    EvaluationReportRecord,
    EvaluationSuiteRecord,
    ExecutionJob,
    ModelProfile,
    ModelProviderType,
    Organization,
    OrganizationLifecycle,
    OrganizationMembership,
    OrganizationModelConfiguration,
    OrganizationSettings,
    Permission,
    PluginDescriptor,
    PluginInstallation,
    Principal,
    Role,
    RoleBinding,
    SkillDefinition,
    SkillPublication,
    SkillRevocation,
    SkillVersion,
    Task,
    TaskMessage,
    TaskTurn,
    ToolInvocation,
    User,
    WaitlistEntry,
    WaitlistSubmission,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTrigger,
    WorkflowVersion,
    Workspace,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from contracts.v1.slack_access import (
    SlackAccessSettings,
    SlackAllowedChannel,
    SlackAllowedUser,
    SlackConnectorHealth,
)
from contracts.v1.workflow_studio import CanvasRun, CanvasSettings
from modules.auth.runtime_organizations import MockModelIdentifier
from modules.limits.service import AdmissionService
from modules.usage.service import UsageSettings
from shared.keyed_locks import KeyedLockRegistry


class RuntimeCompositionInputs(Protocol):
    """Define only the process ports consumed while building capabilities."""

    admission: AdmissionService
    _advisory_transaction_lock: Callable[[str], None] | None
    _bootstrapped: bool
    _execution_job_idempotency_locks: KeyedLockRegistry
    _idempotency_guard: RLock
    _idempotency_locks: KeyedLockRegistry

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the process-configured unit of work."""
        ...

    canvas_runs: Repository[CanvasRun]
    canvas_settings: Repository[CanvasSettings]
    agent_runs: Repository[AgentRun]
    agent_versions: Repository[AgentVersion]
    agents: Repository[AgentDefinition]
    api_credentials: Repository[ApiCredential]
    artifacts: Repository[Artifact]
    audit_events: Repository[AuditEvent]
    accounts: Repository[Account]
    account_identities: Repository[AccountIdentity]
    memberships: Repository[OrganizationMembership]
    account_oauth_states: Repository[AccountOAuthState]
    account_sessions: Repository[AccountSession]
    account_organization_requests: Repository[AccountOrganizationRequest]
    waitlist_entries: Repository[WaitlistEntry]
    waitlist_submissions: Repository[WaitlistSubmission]
    slack_access_settings: Repository[SlackAccessSettings]
    slack_allowed_users: Repository[SlackAllowedUser]
    slack_allowed_channels: Repository[SlackAllowedChannel]
    slack_connector_health: Repository[SlackConnectorHealth]
    evaluation_executions: Repository[EvaluationExecution]
    evaluation_reports: Repository[EvaluationReportRecord]
    evaluation_suites: Repository[EvaluationSuiteRecord]
    execution_jobs: Repository[ExecutionJob]
    tasks: Repository[Task]
    task_messages: Repository[TaskMessage]
    task_turns: Repository[TaskTurn]
    installations: Repository[PluginInstallation]
    model_profiles: Repository[ModelProfile]
    model_configurations: Repository[OrganizationModelConfiguration]
    organizations: Repository[Organization]
    organization_settings: Repository[OrganizationSettings]
    organization_lifecycle: Repository[OrganizationLifecycle]
    plugins: Repository[PluginDescriptor]
    principals: Repository[Principal]
    providers: Repository[ModelProviderContract]
    role_bindings: Repository[RoleBinding]
    skill_definitions: Repository[SkillDefinition]
    skill_versions: Repository[SkillVersion]
    skill_publications: Repository[SkillPublication]
    skill_revocations: Repository[SkillRevocation]
    roles: Repository[Role]
    runs: Repository[WorkflowRun]
    threads: Repository[ConversationThread]
    tool_invocations: Repository[ToolInvocation]
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
    model: MockModelIdentifier
    model_adapters: Mapping[ModelProviderType, ModelAdapter]
    model_gateway: ModelGateway
    openai_enabled: bool
    plugin_gateway: PluginDispatcher
    secret_store: SecretStore
    usage_ledger: UsageLedger
    usage_settings: UsageSettings
    _repository_set: ProductRepositorySet
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
