"""Construct capability services at the API and worker composition boundary."""

from __future__ import annotations

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.agents.bootstrap import AgentBootstrapService
from modules.agents.runtime_lifecycle import AgentService
from modules.models.defaults import default_active_model_profile
from modules.workflows.automation_service import WorkflowAutomationService
from modules.workflows.definition_service import WorkflowDefinitionService


def _configure_workflow_automation_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose workflow automation with persistence and execution ports."""
    services.workflow_automation_service = WorkflowAutomationService(
        audit_events=dependencies.audit_events,
        workflow_triggers=dependencies.workflow_triggers,
        workflow_schedules=dependencies.workflow_schedules,
        workflow_versions=dependencies.workflow_versions,
        workflows=dependencies.workflows,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        unit_of_work=dependencies._unit_of_work,
        advisory_transaction_lock=dependencies._advisory_transaction_lock,
        enqueue_agent_run=dependencies.enqueue_agent_run,
    )


def _configure_workflow_definition_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose workflow definition use cases with database and auth ports."""
    services.workflow_definition_service = WorkflowDefinitionService(
        agent_versions=dependencies.agent_versions,
        agents=dependencies.agents,
        audit_events=dependencies.audit_events,
        runs=dependencies.runs,
        workflow_versions=dependencies.workflow_versions,
        workflows=dependencies.workflows,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        unit_of_work=dependencies._unit_of_work,
    )


def _configure_agent_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose agent lifecycle use cases with policy and persistence ports."""
    services.agent_bootstrap_service = AgentBootstrapService(
        agents=dependencies.agents,
        agent_versions=dependencies.agent_versions,
        workflows=dependencies.workflows,
        workflow_versions=dependencies.workflow_versions,
        default_model_profile_id=dependencies.default_model_profile_id,
        provider_backed=dependencies.openai_enabled,
    )
    services.agent_service = AgentService(
        agents=dependencies.agents,
        agent_versions=dependencies.agent_versions,
        audit_events=dependencies.audit_events,
        model_profiles=dependencies.model_profiles,
        providers=dependencies.providers,
        workflows=dependencies.workflows,
        workflow_versions=dependencies.workflow_versions,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        default_model_profile=lambda: default_active_model_profile(
            dependencies.model_profiles
        ),
        default_approval_policy_id=(
            services.governance_service.default_approval_policy_id
        ),
        capability_installation=(
            services.integration_admin_service.resolve_capability_installation
        ),
        unit_of_work=dependencies._unit_of_work,
    )
