"""Compose tenant administration and its shared transaction guard."""

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.auth.organization_administration import OrganizationAdministrationService
from modules.auth.organization_guard import OrganizationGuard


def configure_organization_administration(
    dependencies: RuntimeCompositionInputs,
    services: CapabilityServiceSet,
) -> None:
    """Share one guard across organization, role and member lifecycle use cases."""
    guard = OrganizationGuard(
        settings=dependencies.organization_settings,
        principals=dependencies.principals,
        users=dependencies.users,
        roles=dependencies.roles,
        bindings=dependencies.role_bindings,
        audit=dependencies.audit_events,
        memberships=dependencies.memberships,
        accounts=dependencies.accounts,
        unit_of_work=dependencies._unit_of_work,
        lock=dependencies._advisory_transaction_lock,
    )
    services.organization_administration_service = OrganizationAdministrationService(
        guard=guard,
        organizations=dependencies.organizations,
        credentials=dependencies.api_credentials,
        authorize=dependencies.authorize_organization,
    )
