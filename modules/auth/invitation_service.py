"""Invitation creation, delivery, acceptance, and revocation use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    Permission,
    Principal,
    Role,
    RoleBinding,
    User,
    UserInvitation,
    UserPasswordCredential,
    UserSession,
    Workspace,
)
from modules.auth.runtime_invitation_acceptance import InvitationAcceptanceMixin
from modules.auth.runtime_invitations import InvitationRuntimeMixin


class InvitationService(InvitationRuntimeMixin, InvitationAcceptanceMixin):
    """Manage invitation lifecycle using scoped repositories and auth ports."""

    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    user_invitations: Repository[UserInvitation]
    user_passwords: Repository[UserPasswordCredential]
    user_sessions: Repository[UserSession]
    users: Repository[User]
    workspaces: Repository[Workspace]

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        principals: Repository[Principal],
        role_bindings: Repository[RoleBinding],
        roles: Repository[Role],
        user_invitations: Repository[UserInvitation],
        user_passwords: Repository[UserPasswordCredential],
        user_sessions: Repository[UserSession],
        users: Repository[User],
        workspaces: Repository[Workspace],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind invitation repositories and application lifecycle callbacks."""
        self.audit_events = audit_events
        self.principals = principals
        self.role_bindings = role_bindings
        self.roles = roles
        self.user_invitations = user_invitations
        self.user_passwords = user_passwords
        self.user_sessions = user_sessions
        self.users = users
        self.workspaces = workspaces
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._authorize_organization = authorize_organization
        self._open_unit_of_work = unit_of_work

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce user and role permissions through the identity capability."""
        self._authorize(principal_id, permission)

    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce organization roles for privilege-bearing invitation actions."""
        self._authorize_organization(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for invitation and account changes."""
        return self._open_unit_of_work()
