"""Bind typed product repositories from the process composition root."""

from __future__ import annotations

from typing import TypeVar, cast

from application.ports.product_repositories import ProductRepositorySet
from application.ports.repository import Repository
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
    OAuthAuthorizationState,
    Organization,
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
from contracts.v1.base import ProductContract

TProduct = TypeVar("TProduct", bound=ProductContract)


class RuntimeRepositoryBindingMixin:
    """Expose product repositories to application runtime collaborators."""

    _repository_set: ProductRepositorySet
    organizations: Repository[Organization]
    workspaces: Repository[Workspace]
    users: Repository[User]
    principals: Repository[Principal]
    api_credentials: Repository[ApiCredential]
    user_passwords: Repository[UserPasswordCredential]
    user_sessions: Repository[UserSession]
    user_invitations: Repository[UserInvitation]
    roles: Repository[Role]
    role_bindings: Repository[RoleBinding]
    providers: Repository[ModelProviderContract]
    model_profiles: Repository[ModelProfile]
    plugins: Repository[PluginDescriptor]
    installations: Repository[PluginInstallation]
    oauth_authorization_states: Repository[OAuthAuthorizationState]
    agents: Repository[AgentDefinition]
    agent_versions: Repository[AgentVersion]
    workflows: Repository[WorkflowDefinition]
    workflow_versions: Repository[WorkflowVersion]
    workflow_triggers: Repository[WorkflowTrigger]
    workflow_schedules: Repository[WorkflowSchedule]
    evaluation_suites: Repository[EvaluationSuiteRecord]
    evaluation_executions: Repository[EvaluationExecution]
    evaluation_reports: Repository[EvaluationReportRecord]
    approval_policies: Repository[ApprovalPolicy]
    approvals: Repository[ApprovalRequest]
    runs: Repository[WorkflowRun]
    agent_runs: Repository[AgentRun]
    tool_invocations: Repository[ToolInvocation]
    artifacts: Repository[Artifact]
    knowledge_documents: Repository[KnowledgeDocument]
    memory_items: Repository[MemoryItem]
    threads: Repository[ConversationThread]
    execution_jobs: Repository[ExecutionJob]
    audit_events: Repository[AuditEvent]

    def _bind_repositories(self) -> None:
        """Attach one repository for each product collection."""
        self.organizations = self._repository("organizations", Organization)
        self.workspaces = self._repository("workspaces", Workspace)
        self.users = self._repository("users", User)
        self.principals = self._repository("principals", Principal)
        self.api_credentials = self._repository("api_credentials", ApiCredential)
        self.user_passwords = self._repository("user_passwords", UserPasswordCredential)
        self.user_sessions = self._repository("user_sessions", UserSession)
        self.user_invitations = self._repository("user_invitations", UserInvitation)
        self.roles = self._repository("roles", Role)
        self.role_bindings = self._repository("role_bindings", RoleBinding)
        self.providers = self._repository("model_providers", ModelProviderContract)
        self.model_profiles = self._repository("model_profiles", ModelProfile)
        self.plugins = self._repository("plugins", PluginDescriptor)
        self.installations = self._repository(
            "plugin_installations", PluginInstallation
        )
        self.oauth_authorization_states = self._repository(
            "oauth_authorization_states", OAuthAuthorizationState
        )
        self.agents = self._repository("agents", AgentDefinition)
        self.agent_versions = self._repository("agent_versions", AgentVersion)
        self.workflows = self._repository("workflows", WorkflowDefinition)
        self.workflow_versions = self._repository("workflow_versions", WorkflowVersion)
        self.workflow_triggers = self._repository("workflow_triggers", WorkflowTrigger)
        self.workflow_schedules = self._repository(
            "workflow_schedules", WorkflowSchedule
        )
        self.evaluation_suites = self._repository(
            "evaluation_suites", EvaluationSuiteRecord
        )
        self.evaluation_executions = self._repository(
            "evaluation_executions", EvaluationExecution
        )
        self.evaluation_reports = self._repository(
            "evaluation_reports", EvaluationReportRecord
        )
        self.approval_policies = self._repository("approval_policies", ApprovalPolicy)
        self.approvals = self._repository("approval_requests", ApprovalRequest)
        self.runs = self._repository("workflow_runs", WorkflowRun)
        self.agent_runs = self._repository("agent_runs", AgentRun)
        self.tool_invocations = self._repository("tool_invocations", ToolInvocation)
        self.artifacts = self._repository("artifacts", Artifact)
        self.knowledge_documents = self._repository(
            "knowledge_documents", KnowledgeDocument
        )
        self.memory_items = self._repository("memory_items", MemoryItem)
        self.threads = self._repository("conversation_threads", ConversationThread)
        self.execution_jobs = self._repository("execution_jobs", ExecutionJob)
        self.audit_events = self._repository("audit_events", AuditEvent)

    def _repository(
        self, collection: str, record_type: type[TProduct]
    ) -> Repository[TProduct]:
        """Resolve a repository by collection while preserving its record type."""
        del record_type
        return cast(Repository[TProduct], self._repository_set.get(collection))
