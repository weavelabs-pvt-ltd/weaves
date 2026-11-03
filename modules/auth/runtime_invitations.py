"""Workspace invitation lifecycle and acceptance use cases."""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    User,
    UserInvitation,
    UserInvitationStatus,
    UserPasswordCredential,
    UserSession,
    UserStatus,
    Workspace,
    WorkspaceStatus,
)
from modules.auth.role_presets import validate_role_preset_scope
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


@dataclass(frozen=True)
class InvitationBundle:
    """Records created together when a workspace user is invited."""

    user: User
    principal: Principal
    binding: RoleBinding
    invitation: UserInvitation
    token: str
    timestamp: Any


class InvitationRuntimeHost(ABC):
    """Dependencies for creating, delivering, and accepting invitations."""

    _bootstrapped: Callable[[], bool]
    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    user_invitations: Repository[UserInvitation]
    user_passwords: Repository[UserPasswordCredential]
    user_sessions: Repository[UserSession]
    users: Repository[User]
    workspaces: Repository[Workspace]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before invitation operations."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workspace permissions for invitation management."""
        raise NotImplementedError

    @abstractmethod
    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce organization permissions for custom role provisioning."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for related invitation and account writes."""
        raise NotImplementedError


class InvitationRuntimeMixin(InvitationRuntimeHost):
    """Implement invitation lifecycle and one-time account activation."""

    def create_user_invitation(
        self,
        *,
        email: str,
        display_name: str,
        permissions: tuple[Permission, ...] = (),
        role_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[User, Principal, Role, UserInvitation, str]:
        """Provision a disabled user and issue a one-time invitation token."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.USERS_MANAGE)
        self.authorize(actor, Permission.ROLES_MANAGE)
        if role_id is None:
            self.authorize_organization(actor, Permission.USERS_MANAGE)
            self.authorize_organization(actor, Permission.ROLES_MANAGE)
        workspace = self.workspaces.get_scoped(
            effective_workspace_id(), effective_org_id()
        )
        if workspace.status is not WorkspaceStatus.ACTIVE:
            raise ValueError("invitations require an active workspace")
        normalized_email = email.strip().casefold()
        self._ensure_email_is_available(normalized_email)
        role = self._resolve_invitation_role(display_name, role_id, permissions)
        bundle = self._build_invitation_bundle(
            workspace, role, normalized_email, display_name
        )
        self._persist_invitation(
            bundle.user,
            bundle.principal,
            role,
            role_id,
            bundle.binding,
            bundle.invitation,
            workspace.id,
            actor,
            bundle.timestamp,
        )
        return (
            bundle.user,
            bundle.principal,
            role,
            bundle.invitation,
            bundle.token,
        )

    def _ensure_email_is_available(self, normalized_email: str) -> None:
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("a valid user email is required")
        if any(
            user.email.casefold() == normalized_email
            and user.status is not UserStatus.DELETED
            for user in self.users.list_scoped(effective_org_id())
        ):
            raise ValueError("email already belongs to a user in this organization")

    @staticmethod
    def _build_invitation_bundle(
        workspace: Workspace,
        role: Role,
        email: str,
        display_name: str,
    ) -> InvitationBundle:
        timestamp = _now()
        user = User(
            id=_id(),
            org_id=effective_org_id(),
            email=email,
            display_name=display_name,
            status=UserStatus.INVITED,
            created_at=timestamp,
            updated_at=timestamp,
        )
        principal = Principal(
            id=_id(),
            org_id=effective_org_id(),
            principal_type=PrincipalType.USER,
            status=PrincipalStatus.DISABLED,
            user_id=user.id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        binding = RoleBinding(
            id=_id(),
            org_id=effective_org_id(),
            principal_id=principal.id,
            role_id=role.id,
            workspace_id=workspace.id,
            created_at=timestamp,
        )
        token = secrets.token_urlsafe(32)
        invitation = UserInvitation(
            invitation_id=_id(),
            org_id=effective_org_id(),
            workspace_id=workspace.id,
            user_id=user.id,
            principal_id=principal.id,
            email=email,
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            status=UserInvitationStatus.PENDING,
            expires_at=timestamp + timedelta(days=7),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return InvitationBundle(user, principal, binding, invitation, token, timestamp)

    def _resolve_invitation_role(
        self,
        display_name: str,
        role_id: str | None,
        permissions: tuple[Permission, ...],
    ) -> Role:
        if role_id is not None:
            if permissions:
                raise ValueError("choose a role_id or permissions, not both")
            role = self.roles.get_scoped(role_id, effective_org_id())
            validate_role_preset_scope(role, effective_workspace_id())
            return role
        if not permissions:
            raise ValueError("workspace user must have a role or permissions")
        if len(permissions) != len(set(permissions)):
            raise ValueError("workspace user permissions must be unique")
        return Role(
            id=_id(),
            org_id=effective_org_id(),
            name=f"{display_name} workspace role"[:160],
            permissions=list(permissions),
        )

    def _persist_invitation(
        self,
        user: User,
        principal: Principal,
        role: Role,
        role_id: str | None,
        binding: RoleBinding,
        invitation: UserInvitation,
        workspace_id: str,
        requested_by_principal_id: str,
        timestamp: Any,
    ) -> None:
        with self._unit_of_work():
            self.users.create(user)
            self.principals.create(principal)
            if role_id is None:
                self.roles.create(role)
            self.role_bindings.create(binding)
            self.user_invitations.create(invitation)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=user.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="user.invited",
                    target_type="user",
                    target_id=user.id,
                    summary="Workspace user invitation created",
                    request_id=invitation.invitation_id,
                    metadata={"invitation_id": invitation.invitation_id},
                    created_at=timestamp,
                )
            )

    def list_user_invitations(self) -> tuple[UserInvitation, ...]:
        """List invitations in the selected workspace for user administrators."""
        if not self._bootstrapped():
            self.bootstrap()
        self.authorize(
            effective_principal_id("local-developer"), Permission.USERS_MANAGE
        )
        return tuple(
            sorted(
                self.user_invitations.list_scoped(
                    effective_org_id(), effective_workspace_id()
                ),
                key=lambda item: item.created_at,
                reverse=True,
            )
        )

    def resend_user_invitation(self, invitation_id: str) -> tuple[UserInvitation, str]:
        """Rotate a pending invitation token and restart its seven-day window."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id("local-developer")
        self.authorize(actor, Permission.USERS_MANAGE)
        invitation = self.user_invitations.get_scoped(
            invitation_id, effective_org_id(), effective_workspace_id()
        )
        if invitation.status is not UserInvitationStatus.PENDING:
            raise ValueError("only pending invitations can be resent")
        user = self.users.get_scoped(invitation.user_id, invitation.org_id)
        if user.status is not UserStatus.INVITED:
            raise ValueError("invited user is no longer eligible for acceptance")
        token = secrets.token_urlsafe(32)
        timestamp = _now()
        replacement = UserInvitation.model_validate(
            invitation.model_copy(
                update={
                    "token_digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "expires_at": timestamp + timedelta(days=7),
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            if not self.user_invitations.put_if_invitation_pending(
                replacement, expected_token_digest=invitation.token_digest
            ):
                raise ValueError("invitation changed; fetch it and retry")
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=invitation.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=invitation.workspace_id,
                    actor_principal_id=actor,
                    action="user.invitation_resent",
                    target_type="user_invitation",
                    target_id=invitation.invitation_id,
                    summary="Workspace user invitation token rotated",
                    request_id=invitation.invitation_id,
                    created_at=timestamp,
                )
            )
        return replacement, token

    def revoke_user_invitation(self, invitation_id: str) -> UserInvitation:
        """Revoke an unaccepted invite and disable its pending account."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id("local-developer")
        self.authorize(actor, Permission.USERS_MANAGE)
        invitation = self.user_invitations.get_scoped(
            invitation_id, effective_org_id(), effective_workspace_id()
        )
        if invitation.status is not UserInvitationStatus.PENDING:
            raise ValueError("only pending invitations can be revoked")
        timestamp = _now()
        revoked = UserInvitation.model_validate(
            invitation.model_copy(
                update={
                    "status": UserInvitationStatus.REVOKED,
                    "revoked_at": timestamp,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        user = self.users.get_scoped(invitation.user_id, invitation.org_id)
        principal = self.principals.get_scoped(
            invitation.principal_id, invitation.org_id
        )
        deleted_user = User.model_validate(
            user.model_copy(
                update={"status": UserStatus.DELETED, "updated_at": timestamp}
            ).model_dump()
        )
        disabled_principal = Principal.model_validate(
            principal.model_copy(
                update={"status": PrincipalStatus.DISABLED, "updated_at": timestamp}
            ).model_dump()
        )
        with self._unit_of_work():
            if not self.user_invitations.put_if_invitation_pending(
                revoked, expected_token_digest=invitation.token_digest
            ):
                raise ValueError("invitation changed; fetch it and retry")
            self.users.put(deleted_user)
            self.principals.put(disabled_principal)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=invitation.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=invitation.workspace_id,
                    actor_principal_id=actor,
                    action="user.invitation_revoked",
                    target_type="user_invitation",
                    target_id=invitation.invitation_id,
                    summary="Workspace user invitation revoked",
                    request_id=invitation.invitation_id,
                    created_at=timestamp,
                )
            )
        return revoked
