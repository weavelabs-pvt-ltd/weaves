"""Construct capability services at the API and worker composition boundary."""

from __future__ import annotations

from apps.capability_services import CapabilityServiceSet
from apps.runtime_composition_inputs import RuntimeCompositionInputs
from modules.auth.account_organizations import AccountOrganizationService
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.authentication_service import AuthenticationService
from modules.auth.bootstrap import IdentityBootstrapService
from modules.auth.credential_service import CredentialService
from modules.auth.organization_service import OrganizationService
from modules.auth.user_administration_service import UserAdministrationService
from modules.auth.workspace_access_service import WorkspaceAccessService


def _configure_authentication_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose API-token authentication with auth persistence ports."""
    services.authentication_service = AuthenticationService(
        api_credentials=dependencies.api_credentials,
        principals=dependencies.principals,
        users=dependencies.users,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authenticate_principal=dependencies.authenticate_principal,
    )


def _configure_account_sign_in_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose global account email sign-in and account sessions."""
    services.account_sign_in_service = AccountSignInService(
        accounts=dependencies.accounts,
        identities=dependencies.account_identities,
        challenges=dependencies.account_login_challenges,
        sessions=dependencies.account_sessions,
        memberships=dependencies.memberships,
        users=dependencies.users,
        principals=dependencies.principals,
        organizations=dependencies.organizations,
        workspaces=dependencies.workspaces,
        audit_events=dependencies.audit_events,
        email_sender=dependencies.email_sender,
        email_login_hmac_key=dependencies.email_login_hmac_key,
        login_rate_limiter=dependencies.login_rate_limiter,
        unit_of_work=dependencies._unit_of_work,
    )


def _configure_identity_bootstrap_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose local identity seeds with identity-owned repositories."""
    services.identity_bootstrap_service = IdentityBootstrapService(
        users=dependencies.users,
        principals=dependencies.principals,
        roles=dependencies.roles,
        organizations=dependencies.organizations,
        workspaces=dependencies.workspaces,
        role_bindings=dependencies.role_bindings,
        unit_of_work=dependencies._unit_of_work,
        advisory_transaction_lock=dependencies._advisory_transaction_lock,
    )


def _configure_credential_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose credential lifecycle and authorization with repository ports."""
    services.credential_service = CredentialService(
        api_credentials=dependencies.api_credentials,
        audit_events=dependencies.audit_events,
        organizations=dependencies.organizations,
        principals=dependencies.principals,
        role_bindings=dependencies.role_bindings,
        roles=dependencies.roles,
        workspaces=dependencies.workspaces,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        unit_of_work=dependencies._unit_of_work,
    )


def _configure_user_administration_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose workspace user management with identity and storage ports."""
    services.user_administration_service = UserAdministrationService(
        api_credentials=dependencies.api_credentials,
        audit_events=dependencies.audit_events,
        memberships=dependencies.memberships,
        principals=dependencies.principals,
        role_bindings=dependencies.role_bindings,
        roles=dependencies.roles,
        users=dependencies.users,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        authorize_organization=dependencies.authorize_organization,
        unit_of_work=dependencies._unit_of_work,
    )


def _configure_workspace_access_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose workspace and role management with auth and storage ports."""
    services.workspace_access_service = WorkspaceAccessService(
        approval_policies=dependencies.approval_policies,
        audit_events=dependencies.audit_events,
        principals=dependencies.principals,
        role_bindings=dependencies.role_bindings,
        roles=dependencies.roles,
        workspaces=dependencies.workspaces,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        authorize=dependencies.authorize,
        authorize_organization=dependencies.authorize_organization,
        unit_of_work=dependencies._unit_of_work,
        advisory_transaction_lock=dependencies._advisory_transaction_lock,
    )


def _configure_organization_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose organization onboarding with workspace and identity ports."""
    services.organization_service = OrganizationService(
        api_credentials=dependencies.api_credentials,
        approval_policies=dependencies.approval_policies,
        audit_events=dependencies.audit_events,
        model_profiles=dependencies.model_profiles,
        organizations=dependencies.organizations,
        principals=dependencies.principals,
        providers=dependencies.providers,
        role_bindings=dependencies.role_bindings,
        roles=dependencies.roles,
        users=dependencies.users,
        workspaces=dependencies.workspaces,
        model=dependencies.model,
        is_bootstrapped=lambda: dependencies._bootstrapped,
        bootstrap=dependencies.bootstrap,
        list_principal_workspaces=(
            services.workspace_access_service.list_principal_workspaces
        ),
        unit_of_work=dependencies._unit_of_work,
    )


def _configure_account_organization_service(
    dependencies: RuntimeCompositionInputs, services: CapabilityServiceSet
) -> None:
    """Compose account-created organizations over the tenant provisioner."""
    services.account_organization_service = AccountOrganizationService(
        organization_service=services.organization_service,
        identities=dependencies.account_identities,
        memberships=dependencies.memberships,
        requests=dependencies.account_organization_requests,
        login_rate_limiter=dependencies.login_rate_limiter,
        unit_of_work=dependencies._unit_of_work,
        advisory_transaction_lock=dependencies._advisory_transaction_lock,
    )
