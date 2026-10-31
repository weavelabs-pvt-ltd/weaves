"""Construct capability services at the API and worker composition boundary."""

from __future__ import annotations

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.integrations.admin_service import IntegrationAdminService
from modules.integrations.bootstrap import IntegrationBootstrapService
from modules.integrations.connection_service import IntegrationConnectionService


def _configure_integration_services(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Construct integration capabilities with shared process resources."""
    _configure_integration_bootstrap_service(dependencies, services)
    _configure_integration_admin_service(dependencies, services)
    _configure_integration_connection_service(dependencies, services)


def _configure_integration_bootstrap_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose built-in integration seeds from module-owned repositories."""
    services.integration_bootstrap_service = IntegrationBootstrapService(
        plugins=dependencies.plugins,
        installations=dependencies.installations,
        approval_policies=dependencies.approval_policies,
    )


def _configure_integration_admin_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose integration administration with managed adapter resources."""
    services.integration_admin_service = IntegrationAdminService(
        audit_events=dependencies.audit_events,
        installations=dependencies.installations,
        plugins=dependencies.plugins,
        knowledge_documents=dependencies.knowledge_documents,
        secret_store=dependencies.secret_store,
        github_plugin=dependencies.github_plugin,
        slack_plugin=dependencies.slack_plugin,
        jira_plugin=dependencies.jira_plugin,
        mcp_plugin=dependencies.mcp_plugin,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
    )


def _configure_integration_connection_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose connector installation creation with runtime ports."""
    services.integration_connection_service = IntegrationConnectionService(
        audit_events=dependencies.audit_events,
        installations=dependencies.installations,
        plugins=dependencies.plugins,
        secret_store=dependencies.secret_store,
        mcp_plugin=dependencies.mcp_plugin,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
    )
