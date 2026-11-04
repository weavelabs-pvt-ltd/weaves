"""Construct capability services at the API and worker composition boundary."""

from __future__ import annotations

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.agents.defaults import default_active_agent_id
from modules.knowledge.bootstrap import KnowledgeBootstrapService
from modules.knowledge.runtime import KnowledgeService
from modules.memory.runtime import MemoryService
from modules.models.bootstrap import ModelBootstrapService
from modules.models.catalog import ModelCatalogService
from modules.models.runtime import ModelService


def _configure_model_and_context_services(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Construct model, knowledge, and conversation memory services."""
    services.model_bootstrap_service = ModelBootstrapService(
        providers=dependencies.providers,
        profiles=dependencies.model_profiles,
        mock_model=dependencies.model,
        default_profile_id=dependencies.default_model_profile_id,
        environment_enabled=dependencies.environment_model_enabled,
        environment_provider_type=dependencies.environment_model_provider_type,
        environment_model_name=dependencies.environment_model_name,
        environment_base_url=dependencies.environment_model_base_url,
        unit_of_work=dependencies._unit_of_work,
    )
    services.knowledge_bootstrap_service = KnowledgeBootstrapService(
        documents=dependencies.knowledge_documents
    )
    services.model_catalog_service = ModelCatalogService(
        audit_events=dependencies.audit_events,
        model_profiles=dependencies.model_profiles,
        providers=dependencies.providers,
        model_adapters=dependencies.model_adapters,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize_organization=dependencies.authorize_organization,
        unit_of_work=dependencies._unit_of_work,
    )
    services.model_service = ModelService(
        audit_events=dependencies.audit_events,
        model_configurations=dependencies.model_configurations,
        model_profiles=dependencies.model_profiles,
        providers=dependencies.providers,
        secret_store=dependencies.secret_store,
        catalog_service=services.model_catalog_service,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        authorize_organization=dependencies.authorize_organization,
        unit_of_work=dependencies._unit_of_work,
        default_model_profile_id=dependencies.default_model_profile_id,
    )
    services.knowledge = KnowledgeService(
        documents=dependencies.knowledge_documents,
        audit_events=dependencies.audit_events,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
    )
    services.memory_service = MemoryService(
        agents=dependencies.agents,
        audit_events=dependencies.audit_events,
        memory_items=dependencies.memory_items,
        threads=dependencies.threads,
        workflows=dependencies.workflows,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        authorize_organization=dependencies.authorize_organization,
        default_agent_id=lambda: default_active_agent_id(dependencies.agents),
        unit_of_work=dependencies._unit_of_work,
    )
