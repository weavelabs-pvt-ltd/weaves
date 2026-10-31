"""Compose the capability services required by API route registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apps.platform_runtime import LocalPlatformRuntime
from modules.agents.execution_service import AgentExecutionService
from modules.agents.runtime_lifecycle import AgentService
from modules.auth.authentication_service import AuthenticationService
from modules.auth.credential_service import CredentialService
from modules.auth.invitation_service import InvitationService
from modules.auth.organization_service import OrganizationService
from modules.auth.user_administration_service import UserAdministrationService
from modules.auth.workspace_access_service import WorkspaceAccessService
from modules.evaluations.service import EvaluationService
from modules.executions.http.routes import ExecutionRouteServices
from modules.governance.service import GovernanceService
from modules.integrations.admin_service import IntegrationAdminService
from modules.integrations.connection_service import IntegrationConnectionService
from modules.integrations.http.oauth_context import OAuthRouteDependencies
from modules.integrations.oauth_state_service import OAuthStateService
from modules.knowledge.runtime import KnowledgeService
from modules.memory.runtime import MemoryService
from modules.models.runtime import ModelService
from modules.workflows.automation_service import WorkflowAutomationService
from modules.workflows.definition_service import WorkflowDefinitionService
from modules.workflows.execution_service import WorkflowExecutionService


@dataclass(frozen=True)
class ApiRouteServices:
    """Hold explicit capability dependencies used when registering API routes."""

    storage_mode: str
    check_storage: Callable[[], None]
    organization: OrganizationService
    workspace_access: WorkspaceAccessService
    user_administration: UserAdministrationService
    invitation: InvitationService
    authentication: AuthenticationService
    credential: CredentialService
    models: ModelService
    integration_admin: IntegrationAdminService
    integration_connection: IntegrationConnectionService
    oauth: OAuthRouteDependencies
    knowledge: KnowledgeService
    memory: MemoryService
    agent_execution: AgentExecutionService
    agents: AgentService
    evaluations: EvaluationService
    workflow_definition: WorkflowDefinitionService
    workflow_automation: WorkflowAutomationService
    workflow_execution: WorkflowExecutionService
    execution: ExecutionRouteServices
    governance: GovernanceService


def build_api_route_services(runtime: LocalPlatformRuntime) -> ApiRouteServices:
    """Resolve API route dependencies from process composition once at startup."""
    oauth = OAuthRouteDependencies(
        http_client=runtime.http_client,
        state_service=OAuthStateService(
            states=runtime.oauth_authorization_states,
            credential_service=runtime.credential_service,
        ),
        secret_store=runtime.secret_store,
        github_app_token_provider=runtime.github_app_token_provider,
        connection_service=runtime.integration_connection_service,
    )
    execution = ExecutionRouteServices(
        agent_execution=runtime.agent_execution_service,
        execution=runtime.execution_service,
        governance=runtime.governance_service,
        durable_execution_enabled=runtime.storage_mode == "postgres",
    )
    return ApiRouteServices(
        storage_mode=runtime.storage_mode,
        check_storage=runtime.check_storage,
        organization=runtime.organization_service,
        workspace_access=runtime.workspace_access_service,
        user_administration=runtime.user_administration_service,
        invitation=runtime.invitation_service,
        authentication=runtime.authentication_service,
        credential=runtime.credential_service,
        models=runtime.model_service,
        integration_admin=runtime.integration_admin_service,
        integration_connection=runtime.integration_connection_service,
        oauth=oauth,
        knowledge=runtime.knowledge,
        memory=runtime.memory_service,
        agent_execution=runtime.agent_execution_service,
        agents=runtime.agent_service,
        evaluations=runtime.evaluation_service,
        workflow_definition=runtime.workflow_definition_service,
        workflow_automation=runtime.workflow_automation_service,
        workflow_execution=runtime.workflow_execution_service,
        execution=execution,
        governance=runtime.governance_service,
    )
