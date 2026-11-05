"""Legacy organization, workspace, and user runtime delegates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from contracts.v1 import (
    Organization,
    Permission,
    Principal,
    Role,
    RoleBinding,
    User,
    Workspace,
    WorkspaceStatus,
)

if TYPE_CHECKING:
    from modules.auth.organization_service import OrganizationService
    from modules.auth.user_administration_service import UserAdministrationService
    from modules.auth.workspace_access_service import WorkspaceAccessService


class RuntimeIdentityCompatibilityMixin:
    """Preserve legacy identity method names while capability services own behavior."""

    organization_service: OrganizationService
    workspace_access_service: WorkspaceAccessService
    user_administration_service: UserAdministrationService

    def create_organization(
        self, *, name: str, email: str, display_name: str
    ) -> tuple[Organization, Workspace, User, Principal, str]:
        """Delegate onboarding and preserve the legacy runtime method."""
        return self.organization_service.create_organization(
            name=name, email=email, display_name=display_name
        )

    def check_onboarding_rate_limit(self, client_address: str | None) -> None:
        """Delegate onboarding throttling to the organization service."""
        self.organization_service.check_onboarding_rate_limit(client_address)

    def resolve_principal_scope(
        self, principal_id: str, workspace_id: str | None = None
    ) -> tuple[str, str]:
        """Delegate principal workspace resolution to the organization service."""
        return self.organization_service.resolve_principal_scope(
            principal_id, workspace_id
        )

    def list_principal_workspaces(self, principal_id: str) -> tuple[Workspace, ...]:
        """Delegate principal workspace visibility to the access service."""
        return self.workspace_access_service.list_principal_workspaces(principal_id)

    def list_managed_workspaces(
        self, requested_by_principal_id: str = "local-developer"
    ) -> tuple[Workspace, ...]:
        """Delegate organization workspace listing to the access service."""
        return self.workspace_access_service.list_managed_workspaces(
            requested_by_principal_id
        )

    def create_workspace(
        self,
        *,
        name: str,
        environment: str = "default",
        requested_by_principal_id: str = "local-developer",
    ) -> Workspace:
        """Delegate workspace provisioning to the access service."""
        return self.workspace_access_service.create_workspace(
            name=name,
            environment=environment,
            requested_by_principal_id=requested_by_principal_id,
        )

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        environment: str | None = None,
        status: WorkspaceStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> Workspace:
        """Delegate workspace lifecycle updates to the access service."""
        return self.workspace_access_service.update_workspace(
            workspace_id,
            name=name,
            environment=environment,
            status=status,
            requested_by_principal_id=requested_by_principal_id,
        )

    def create_role(
        self,
        *,
        name: str,
        permissions: tuple[Permission, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> Role:
        """Delegate role creation to the workspace access service."""
        return self.workspace_access_service.create_role(
            name=name,
            permissions=permissions,
            requested_by_principal_id=requested_by_principal_id,
        )

    def list_roles(self) -> tuple[Role, ...]:
        """Delegate scoped role listing to the workspace access service."""
        return self.workspace_access_service.list_roles()

    def create_role_binding(
        self,
        *,
        principal_id: str,
        role_id: str,
        workspace_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> RoleBinding:
        """Delegate role assignment to the workspace access service."""
        return self.workspace_access_service.create_role_binding(
            principal_id=principal_id,
            role_id=role_id,
            workspace_id=workspace_id,
            requested_by_principal_id=requested_by_principal_id,
        )

    def list_role_bindings(self) -> tuple[RoleBinding, ...]:
        """Delegate role assignment listing to the workspace access service."""
        return self.workspace_access_service.list_role_bindings()

    def delete_role_binding(
        self,
        binding_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> None:
        """Delegate role assignment removal to the workspace access service."""
        self.workspace_access_service.delete_role_binding(
            binding_id, requested_by_principal_id=requested_by_principal_id
        )

    def create_workspace_user(
        self,
        *,
        email: str,
        display_name: str,
        permissions: tuple[Permission, ...] = (),
        role_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[User, Principal, Role]:
        """Delegate workspace user provisioning to its service."""
        return self.user_administration_service.create_workspace_user(
            email=email,
            display_name=display_name,
            permissions=permissions,
            role_id=role_id,
            requested_by_principal_id=requested_by_principal_id,
        )

    def list_workspace_users(self) -> tuple[User, ...]:
        """Delegate workspace user listing to its service."""
        return self.user_administration_service.list_workspace_users()

    def suspend_workspace_user(
        self,
        user_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> User:
        """Delegate user suspension to its service."""
        return self.user_administration_service.suspend_workspace_user(
            user_id, requested_by_principal_id=requested_by_principal_id
        )

    def reactivate_workspace_user(
        self,
        user_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> User:
        """Delegate user reactivation to its service."""
        return self.user_administration_service.reactivate_workspace_user(
            user_id, requested_by_principal_id=requested_by_principal_id
        )
