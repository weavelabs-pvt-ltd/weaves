"""Compose capability services for one API or worker process."""

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from apps.runtime_services_auth import (
    _configure_authentication_service,
    _configure_credential_service,
    _configure_identity_bootstrap_service,
    _configure_invitation_service,
    _configure_organization_service,
    _configure_user_administration_service,
    _configure_workspace_access_service,
)
from apps.runtime_services_context import _configure_model_and_context_services
from apps.runtime_services_execution import (
    _configure_agent_execution_service,
    _configure_evaluation_and_recovery_services,
    _configure_execution_service,
    _configure_execution_worker_service,
    _configure_workflow_execution_service,
)
from apps.runtime_services_governance import _configure_governance_service
from apps.runtime_services_integrations import _configure_integration_services
from apps.runtime_services_workflows import (
    _configure_agent_service,
    _configure_workflow_automation_service,
    _configure_workflow_definition_service,
)


def configure_capability_services(
    dependencies: RuntimeCompositionInputs,
) -> CapabilityServiceSet:
    """Build capability services from explicit infrastructure and lifecycle ports."""
    services = CapabilityServiceSet()
    _configure_identity_bootstrap_service(dependencies, services)
    _configure_credential_service(dependencies, services)
    _configure_authentication_service(dependencies, services)
    _configure_invitation_service(dependencies, services)
    _configure_user_administration_service(dependencies, services)
    _configure_workspace_access_service(dependencies, services)
    _configure_organization_service(dependencies, services)
    _configure_execution_service(dependencies, services)
    _configure_integration_services(dependencies, services)
    _configure_workflow_automation_service(dependencies, services)
    _configure_workflow_definition_service(dependencies, services)
    _configure_governance_service(dependencies, services)
    _configure_agent_service(dependencies, services)
    _configure_agent_execution_service(dependencies, services)
    _configure_workflow_execution_service(dependencies, services)
    _configure_model_and_context_services(dependencies, services)
    _configure_evaluation_and_recovery_services(dependencies, services)
    _configure_execution_worker_service(dependencies, services)
    return services
