"""Workspace user provisioning, suspension, and access management use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    AuditEvent,
    OrganizationMembership,
    Permission,
    Principal,
    Role,
    RoleBinding,
    User,
    UserStatus,
)
from modules.auth.runtime_users import WorkspaceUserRuntimeMixin


class UserAdministrationService(WorkspaceUserRuntimeMixin):
    """Manage workspace user access through identity persistence ports."""

    api_credentials: Repository[ApiCredential]
    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    memberships: Repository[OrganizationMembership]
    users: Repository[User]

    def __init__(
        self,
        *,
        api_credentials: Repository[ApiCredential],
        audit_events: Repository[AuditEvent],
        principals: Repository[Principal],
        role_bindings: Repository[RoleBinding],
        roles: Repository[Role],
        memberships: Repository[OrganizationMembership],
        users: Repository[User],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind user repositories and application lifecycle callbacks."""
        self.api_credentials = api_credentials
        self.audit_events = audit_events
        self.principals = principals
        self.role_bindings = role_bindings
        self.roles = roles
        self.memberships = memberships
        self.users = users
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._authorize_organization = authorize_organization
        self._open_unit_of_work = unit_of_work

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workspace-level user management policy."""
        self._authorize(principal_id, permission)

    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce organization-level user management policy."""
        self._authorize_organization(principal_id, permission)

    def list_workspace_members(self) -> tuple[tuple[User, str], ...]:
        """List visible users paired with principal IDs for access controls."""
        return super().list_workspace_members()

    def account_status(self, user: User) -> str:
        """Report whether the member is suspended, linked to an account, or pending."""
        if user.status is UserStatus.SUSPENDED:
            return "suspended"
        linked = self.memberships.list_by_field("user_id", user.id)
        return "signed_in" if linked else "never"

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for user and credential persistence."""
        return self._open_unit_of_work()
