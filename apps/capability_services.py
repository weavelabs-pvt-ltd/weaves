"""Typed registry populated by the application composition root."""

from modules.agents.bootstrap import AgentBootstrapService
from modules.agents.execution_service import AgentExecutionService
from modules.agents.runtime_lifecycle import AgentService
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
from modules.models.bootstrap import ModelBootstrapService
from modules.models.catalog import ModelCatalogService
from modules.models.runtime import ModelService
from modules.skills.service import SkillsService
from modules.tasks.service import TaskService
from modules.tasks.worker_service import TaskWorkerService
from modules.workflows.automation_service import WorkflowAutomationService
from modules.workflows.definition_service import WorkflowDefinitionService
from modules.workflows.execution_service import WorkflowExecutionService
from modules.workflows.studio_service import WorkflowStudioService


class CapabilityServiceSet:
    """Hold fully constructed capability services for one process."""

    action_recovery_service: ActionRecoveryService
    agent_bootstrap_service: AgentBootstrapService
    agent_execution_service: AgentExecutionService
    agent_service: AgentService
    authentication_service: AuthenticationService
    credential_service: CredentialService
    evaluation_service: EvaluationService
    execution_service: ExecutionService
    execution_worker_service: ExecutionWorkerService
    governance_service: GovernanceService
    identity_bootstrap_service: IdentityBootstrapService
    integration_admin_service: IntegrationAdminService
    integration_bootstrap_service: IntegrationBootstrapService
    integration_connection_service: IntegrationConnectionService
    model_bootstrap_service: ModelBootstrapService
    model_catalog_service: ModelCatalogService
    model_service: ModelService
    organization_service: OrganizationService
    recovery_service: ExecutionRecoveryService
    user_administration_service: UserAdministrationService
    workflow_automation_service: WorkflowAutomationService
    workflow_definition_service: WorkflowDefinitionService
    workflow_studio_service: WorkflowStudioService
    workflow_execution_service: WorkflowExecutionService
    task_service: TaskService
    task_worker_service: TaskWorkerService
    workspace_access_service: WorkspaceAccessService
    skills_service: SkillsService
