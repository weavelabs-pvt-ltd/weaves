"""Construct capability services at the API and worker composition boundary."""

from __future__ import annotations

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.governance.service import GovernanceService


def _configure_governance_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Construct governance with repositories and narrowly scoped callbacks."""
    services.governance_service = GovernanceService(
        approvals=dependencies.approvals,
        approval_policies=dependencies.approval_policies,
        audit_events=dependencies.audit_events,
        agent_runs=dependencies.agent_runs,
        agent_versions=dependencies.agent_versions,
        runs=dependencies.runs,
        tool_invocations=dependencies.tool_invocations,
        plugin_gateway=dependencies.plugin_gateway,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        capability_installation=(
            services.integration_admin_service.resolve_capability_installation
        ),
        unit_of_work=dependencies._unit_of_work,
        approval_lock=dependencies._approval_lock,
    )
