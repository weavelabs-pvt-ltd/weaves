"""Construct repository adapters for the product's durable record collections."""

from __future__ import annotations

from typing import cast

from application.ports.product_repositories import ProductRepositorySet
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
    ApprovalPolicy,
    ApprovalRequest,
    Artifact,
    AuditEvent,
    ConversationThread,
    EvaluationExecution,
    EvaluationReportRecord,
    EvaluationSuiteRecord,
    ExecutionJob,
    ModelProfile,
    OAuthAuthorizationState,
    Organization,
    OrganizationLifecycle,
    OrganizationMembership,
    OrganizationModelConfiguration,
    OrganizationSettings,
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
from contracts.v1.base import ProductContract
from contracts.v1.limits import OrganizationLimitOverride
from contracts.v1.workflow_studio import CanvasRun, CanvasSettings
from infrastructure.database.store import ProductPostgresStore
from infrastructure.persistence.correlated_audit import (
    CorrelatedInMemoryAudit,
    CorrelatedPostgresAudit,
)
from infrastructure.persistence.memory_plugin_catalog import InMemoryPluginCatalog
from infrastructure.persistence.memory_repository import InMemoryRepository
from infrastructure.persistence.organization_data import (
    MemoryOrganizationData,
    PostgresOrganizationData,
)
from infrastructure.persistence.overview_data import (
    MemoryOverviewData,
    PostgresOverviewData,
)
from infrastructure.persistence.postgres_plugin_catalog import PostgresPluginCatalog
from infrastructure.persistence.postgres_repository import PostgresRepository

# Collections whose identifiers are chosen by clients (for example suite names).
# They are stored per organization so tenants never collide on, or probe, a name.
TENANT_KEYED_COLLECTIONS = frozenset({"evaluation_suites", "evaluation_reports"})

REPOSITORY_SPECS: tuple[tuple[str, str, str, type[ProductContract]], ...] = (
    ("organizations", "organizations", "id", Organization),
    ("organization_settings", "organization_settings", "id", OrganizationSettings),
    ("organization_lifecycle", "organization_lifecycle", "id", OrganizationLifecycle),
    ("accounts", "accounts", "id", Account),
    ("account_identities", "account_identities", "id", AccountIdentity),
    ("memberships", "memberships", "id", OrganizationMembership),
    ("account_oauth_states", "account_oauth_states", "id", AccountOAuthState),
    ("account_sessions", "account_sessions", "id", AccountSession),
    (
        "account_organization_requests",
        "account_organization_requests",
        "id",
        AccountOrganizationRequest,
    ),
    ("workspaces", "workspaces", "id", Workspace),
    ("users", "users", "id", User),
    ("principals", "principals", "id", Principal),
    ("api_credentials", "api_credentials", "id", ApiCredential),
    ("roles", "roles", "id", Role),
    ("role_bindings", "role_bindings", "id", RoleBinding),
    ("skill_definitions", "skill_definitions", "skill_id", SkillDefinition),
    ("skill_versions", "skill_versions", "skill_version_id", SkillVersion),
    ("skill_publications", "skill_publications", "publication_id", SkillPublication),
    ("skill_revocations", "skill_revocations", "revocation_id", SkillRevocation),
    ("model_providers", "model_providers", "id", ModelProviderContract),
    ("model_profiles", "model_profiles", "id", ModelProfile),
    (
        "model_configurations",
        "model_configurations",
        "org_id",
        OrganizationModelConfiguration,
    ),
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
        "conversation_threads",
        "conversation_threads",
        "thread_id",
        ConversationThread,
    ),
    ("execution_jobs", "execution_jobs", "job_id", ExecutionJob),
    ("canvas_runs", "canvas_runs", "run_id", CanvasRun),
    ("canvas_settings", "canvas_settings", "workflow_id", CanvasSettings),
    ("tasks", "tasks", "task_id", Task),
    ("task_messages", "task_messages", "message_id", TaskMessage),
    ("task_turns", "task_turns", "turn_id", TaskTurn),
    ("audit_events", "audit_events", "audit_event_id", AuditEvent),
    ("organization_limits", "organization_limits", "org_id", OrganizationLimitOverride),
    ("waitlist_entries", "waitlist_entries", "id", WaitlistEntry),
    ("waitlist_submissions", "waitlist_submissions", "id", WaitlistSubmission),
)


def create_product_repository_set(
    store: ProductPostgresStore | None,
) -> ProductRepositorySet:
    """Build all product repositories over one process-selected adapter."""
    repositories = {
        name: _create_repository(store, collection, id_field, record_type)
        for name, collection, id_field, record_type in REPOSITORY_SPECS
    }
    if store is None:
        jobs = cast(InMemoryRepository[ExecutionJob], repositories["execution_jobs"])
        turns = cast(InMemoryRepository[TaskTurn], repositories["task_turns"])
        jobs.share_fair_claims(turns)
    repositories["plugins"] = (
        PostgresPluginCatalog(store)
        if store is not None
        else InMemoryPluginCatalog(
            cast(
                InMemoryRepository[PluginInstallation],
                repositories["plugin_installations"],
            )
        )
    )
    repository_set = ProductRepositorySet(repositories)
    repository_set.overview_data = (
        PostgresOverviewData(store)
        if store is not None
        else MemoryOverviewData(repository_set)
    )
    repository_set.organization_data = (
        PostgresOrganizationData(store)
        if store is not None
        else MemoryOrganizationData(repository_set)
    )
    return repository_set


def _create_repository(
    store: ProductPostgresStore | None,
    collection: str,
    id_field: str,
    record_type: type[ProductContract],
) -> object:
    """Choose an in-memory or PostgreSQL implementation for one collection."""
    keyed = collection in TENANT_KEYED_COLLECTIONS
    if collection == "audit_events":
        if store is None:
            return CorrelatedInMemoryAudit(id_field, tenant_keyed=keyed)
        return CorrelatedPostgresAudit(
            store, collection, id_field, AuditEvent, tenant_keyed=keyed
        )
    if store is None:
        return InMemoryRepository(id_field, tenant_keyed=keyed)
    return PostgresRepository(
        store, collection, id_field, record_type, tenant_keyed=keyed
    )
