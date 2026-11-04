"""Construct repository adapters for the product's durable record collections."""

from __future__ import annotations

from application.ports.product_repositories import ProductRepositorySet
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
    Task,
    TaskMessage,
    TaskTurn,
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
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.memory_repository import InMemoryRepository
from infrastructure.persistence.postgres_repository import PostgresRepository

REPOSITORY_SPECS: tuple[tuple[str, str, str, type[ProductContract]], ...] = (
    ("organizations", "organizations", "id", Organization),
    ("workspaces", "workspaces", "id", Workspace),
    ("users", "users", "id", User),
    ("principals", "principals", "id", Principal),
    ("api_credentials", "api_credentials", "id", ApiCredential),
    ("user_passwords", "user_passwords", "id", UserPasswordCredential),
    ("user_sessions", "user_sessions", "id", UserSession),
    ("user_invitations", "user_invitations", "invitation_id", UserInvitation),
    ("roles", "roles", "id", Role),
    ("role_bindings", "role_bindings", "id", RoleBinding),
    ("model_providers", "model_providers", "id", ModelProviderContract),
    ("model_profiles", "model_profiles", "id", ModelProfile),
    ("plugins", "plugins", "plugin_id", PluginDescriptor),
    (
        "plugin_installations",
        "plugin_installations",
        "plugin_installation_id",
        PluginInstallation,
    ),
    (
        "oauth_authorization_states",
        "oauth_authorization_states",
        "oauth_state_id",
        OAuthAuthorizationState,
    ),
    ("agents", "agents", "id", AgentDefinition),
    ("agent_versions", "agent_versions", "agent_version_id", AgentVersion),
    ("workflows", "workflows", "workflow_id", WorkflowDefinition),
    (
        "workflow_versions",
        "workflow_versions",
        "workflow_version_id",
        WorkflowVersion,
    ),
    ("workflow_triggers", "workflow_triggers", "trigger_id", WorkflowTrigger),
    (
        "workflow_schedules",
        "workflow_schedules",
        "schedule_id",
        WorkflowSchedule,
    ),
    (
        "evaluation_suites",
        "evaluation_suites",
        "suite_id",
        EvaluationSuiteRecord,
    ),
    (
        "evaluation_executions",
        "evaluation_executions",
        "evaluation_id",
        EvaluationExecution,
    ),
    (
        "evaluation_reports",
        "evaluation_reports",
        "evaluation_id",
        EvaluationReportRecord,
    ),
    (
        "approval_policies",
        "approval_policies",
        "approval_policy_id",
        ApprovalPolicy,
    ),
    (
        "approval_requests",
        "approval_requests",
        "approval_request_id",
        ApprovalRequest,
    ),
    ("workflow_runs", "workflow_runs", "run_id", WorkflowRun),
    ("agent_runs", "agent_runs", "agent_run_id", AgentRun),
    ("tool_invocations", "tool_invocations", "invocation_id", ToolInvocation),
    ("artifacts", "artifacts", "artifact_id", Artifact),
    (
        "knowledge_documents",
        "knowledge_documents",
        "document_id",
        KnowledgeDocument,
    ),
    ("memory_items", "memory_items", "memory_id", MemoryItem),
    (
        "conversation_threads",
        "conversation_threads",
        "thread_id",
        ConversationThread,
    ),
    ("execution_jobs", "execution_jobs", "job_id", ExecutionJob),
    ("tasks", "tasks", "task_id", Task),
    ("task_messages", "task_messages", "message_id", TaskMessage),
    ("task_turns", "task_turns", "turn_id", TaskTurn),
    ("audit_events", "audit_events", "audit_event_id", AuditEvent),
)


def create_product_repository_set(
    store: ProductPostgresStore | None,
) -> ProductRepositorySet:
    """Build all product repositories over one process-selected adapter."""
    repositories = {
        name: _create_repository(store, collection, id_field, record_type)
        for name, collection, id_field, record_type in REPOSITORY_SPECS
    }
    return ProductRepositorySet(repositories)


def _create_repository(
    store: ProductPostgresStore | None,
    collection: str,
    id_field: str,
    record_type: type[ProductContract],
) -> object:
    """Choose an in-memory or PostgreSQL implementation for one collection."""
    if store is None:
        return InMemoryRepository(id_field)
    return PostgresRepository(store, collection, id_field, record_type)
