"""Compose tenant administration and its shared transaction guard."""

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.auth.organization_administration import OrganizationAdministrationService
from modules.auth.organization_export import (
    OrganizationExportService,
    max_bytes_from_env,
)
from modules.auth.organization_guard import OrganizationGuard
from modules.auth.organization_lifecycle import (
    OrganizationLifecycleService,
    grace_days_from_env,
)
from modules.auth.organization_purge import OrganizationPurgeService
from modules.auth.organization_slugs import OrganizationSlugs


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
        slugs=OrganizationSlugs(
            dependencies.organization_settings,
            dependencies._advisory_transaction_lock,
        ),
    )
    lifecycle = OrganizationLifecycleService(
        guard=guard,
        organizations=dependencies.organizations,
        lifecycle=dependencies.organization_lifecycle,
        authorize=dependencies.authorize_organization,
        grace_days=grace_days_from_env(),
    )
    data = dependencies._repository_set.organization_data
    assert data is not None, "organization data port is required"
    services.organization_lifecycle_service = lifecycle
    services.organization_export_service = OrganizationExportService(
        lifecycle=lifecycle,
        data=data,
        usage=dependencies.usage_ledger,
        max_bytes=max_bytes_from_env(),
    )
    services.organization_purge_service = OrganizationPurgeService(
        lifecycle=lifecycle,
        lifecycle_records=dependencies.organization_lifecycle,
        data=data,
        secrets=dependencies.secret_store,
        credentials=dependencies.api_credentials,
        usage=dependencies.usage_ledger,
    )
