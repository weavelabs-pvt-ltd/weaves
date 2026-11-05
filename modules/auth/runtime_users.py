"""Workspace user lifecycle and account suspension use cases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    AuditEvent,
    AuditTargetScope,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    User,
    UserSession,
    UserSessionStatus,
    UserStatus,
)
from modules.auth.role_presets import validate_role_preset_scope
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkspaceUserRuntimeHost(ABC):
    """Dependencies required to manage workspace users and active access."""

    _bootstrapped: Callable[[], bool]
    api_credentials: Repository[ApiCredential]
    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    user_sessions: Repository[UserSession]
    users: Repository[User]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before user operations."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workspace-scoped user management permission."""
        raise NotImplementedError

    @abstractmethod
    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce organization-wide user management permission."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for user and credential changes."""
        raise NotImplementedError


class WorkspaceUserRuntimeMixin(WorkspaceUserRuntimeHost):
    """Implement user activation, suspension, and workspace access."""

    def create_workspace_user(
        self,
        *,
        email: str,
        display_name: str,
        permissions: tuple[Permission, ...] = (),
        role_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[User, Principal, Role]:
        """Provision a human account and workspace role for email sign-in."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.USERS_MANAGE)
        self.authorize(requested_by_principal_id, Permission.ROLES_MANAGE)
        if role_id is None:
            self.authorize_organization(
                requested_by_principal_id, Permission.USERS_MANAGE
            )
            self.authorize_organization(
                requested_by_principal_id, Permission.ROLES_MANAGE
            )
        normalized_email = email.strip().casefold()
        self._ensure_user_email_available(normalized_email)
        role = self._resolve_workspace_user_role(display_name, role_id, permissions)
        timestamp = _now()
        user = User(
            id=_id(),
            org_id=effective_org_id(),
            email=normalized_email,
            display_name=display_name,
            status=UserStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        principal = Principal(
            id=_id(),
            org_id=effective_org_id(),
            principal_type=PrincipalType.USER,
            status=PrincipalStatus.ACTIVE,
            user_id=user.id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        binding = RoleBinding(
            id=_id(),
            org_id=effective_org_id(),
            principal_id=principal.id,
            role_id=role.id,
            workspace_id=effective_workspace_id(),
            created_at=timestamp,
        )
        self._persist_workspace_user(
            binding,
            principal,
            requested_by_principal_id,
            role,
            timestamp,
            user,
            role_id,
        )
        return user, principal, role

    def _ensure_user_email_available(self, normalized_email: str) -> None:
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("a valid user email is required")
        if any(
            item.email.casefold() == normalized_email
            and item.status is not UserStatus.DELETED
            for item in self.users.list_scoped(effective_org_id())
        ):
            raise ValueError("email already belongs to a user in this organization")

    def _resolve_workspace_user_role(
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

    def _persist_workspace_user(
        self,
        binding: Any,
        principal: Any,
        requested_by_principal_id: Any,
        role: Any,
        timestamp: Any,
        user: Any,
        role_id: Any,
    ) -> None:
        """Persist a new workspace user and membership."""
        with self._unit_of_work():
            self.users.create(user)
            self.principals.create(principal)
            if role_id is None:
                self.roles.create(role)
            self.role_bindings.create(binding)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=effective_workspace_id(),
                    actor_principal_id=requested_by_principal_id,
                    action="user.created",
                    target_type="user",
                    target_id=user.id,
                    summary="Workspace user and access role provisioned",
                    request_id=user.id,
                    metadata={
                        "principal_id": principal.id,
                        "role_id": role.id,
                        "permissions": [
                            permission.value for permission in role.permissions
                        ],
                    },
                    created_at=timestamp,
                )
            )

    def list_workspace_users(self) -> tuple[User, ...]:
        """List users in the current workspace and, for org admins, org scope."""
        return tuple(user for user, _ in self.list_workspace_members())

    def list_workspace_members(self) -> tuple[tuple[User, str], ...]:
        """List visible members with their principals for role assignment UI."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id("local-developer")
        self.authorize(actor, Permission.USERS_MANAGE)
        try:
            self.authorize_organization(actor, Permission.USERS_MANAGE)
            can_view_organization_users = True
        except PermissionError:
            can_view_organization_users = False
        org_id = effective_org_id()
        workspace_id = effective_workspace_id()
        bindings = self.role_bindings.list_scoped(org_id)
        principals = {item.id: item for item in self.principals.list_scoped(org_id)}
        visible_user_ids = {
            principal.user_id
            for binding in bindings
            if (
                binding.workspace_id == workspace_id
                or (can_view_organization_users and binding.workspace_id is None)
            )
            and (principal := principals.get(binding.principal_id)) is not None
            and principal.user_id is not None
        }
        visible_users = tuple(
            sorted(
                (
                    user
                    for user in self.users.list_scoped(org_id)
                    if user.id in visible_user_ids
                ),
                key=lambda user: user.email,
            )
        )
        principal_ids = {
            principal.user_id: principal.id
            for principal in principals.values()
            if principal.user_id in visible_user_ids
        }
        return tuple(
            (user, principal_ids[user.id])
            for user in visible_users
            if user.id in principal_ids
        )

    def suspend_workspace_user(
        self,
        user_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> User:
        """Disable a visible workspace user and revoke every active credential."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(requested_by_principal_id, Permission.USERS_MANAGE)
        user = next(
            (item for item in self.list_workspace_users() if item.id == user_id), None
        )
        if user is None:
            raise KeyError("user not found")
        principal = self.principals.find_by_field("user_id", user.id)
        if principal is None or principal.org_id != effective_org_id():
            raise KeyError("user principal not found")
        if user.status is UserStatus.SUSPENDED:
            return user
        timestamp = _now()
        suspended_user = User.model_validate(
            user.model_copy(
                update={"status": UserStatus.SUSPENDED, "updated_at": timestamp}
            ).model_dump()
        )
        disabled_principal = Principal.model_validate(
            principal.model_copy(
                update={"status": PrincipalStatus.DISABLED, "updated_at": timestamp}
            ).model_dump()
        )
        active_credentials = tuple(
            item
            for item in self.api_credentials.list_scoped(effective_org_id())
            if item.principal_id == principal.id
            and item.status is ApiCredentialStatus.ACTIVE
        )
        active_sessions = tuple(
            session
            for session in self.user_sessions.list_scoped(effective_org_id())
            if session.principal_id == principal.id
            and session.status is UserSessionStatus.ACTIVE
        )
        self._suspend_workspace_user_records(
            active_credentials,
            active_sessions,
            disabled_principal,
            principal,
            requested_by_principal_id,
            suspended_user,
            timestamp,
            user,
        )
        return suspended_user

    def _suspend_workspace_user_records(
        self,
        active_credentials: Any,
        active_sessions: Any,
        disabled_principal: Any,
        principal: Any,
        requested_by_principal_id: Any,
        suspended_user: Any,
        timestamp: Any,
        user: Any,
    ) -> None:
        """Persist the suspended user and its audit event."""
        with self._unit_of_work():
            self.users.put(suspended_user)
            self.principals.put(disabled_principal)
            for credential in active_credentials:
                self.api_credentials.put(
                    ApiCredential.model_validate(
                        credential.model_copy(
                            update={
                                "status": ApiCredentialStatus.REVOKED,
                                "revoked_at": timestamp,
                                "updated_at": timestamp,
                            }
                        ).model_dump()
                    )
                )
            for session in active_sessions:
                self.user_sessions.put(
                    UserSession.model_validate(
                        session.model_copy(
                            update={
                                "status": UserSessionStatus.REVOKED,
                                "revoked_at": timestamp,
                                "updated_at": timestamp,
                            }
                        ).model_dump()
                    )
                )
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=effective_workspace_id(),
                    actor_principal_id=requested_by_principal_id,
                    action="user.suspended",
                    target_type="user",
                    target_id=user.id,
                    summary="Workspace user access disabled",
                    request_id=user.id,
                    metadata={
                        "principal_id": principal.id,
                        "access_revoked_count": len(active_credentials),
                        "session_revoked_count": len(active_sessions),
                    },
                    created_at=timestamp,
                )
            )

    def reactivate_workspace_user(
        self,
        user_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> User:
        """Restore a suspended user so they can sign in with organization email."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(requested_by_principal_id, Permission.USERS_MANAGE)
        user = next(
            (item for item in self.list_workspace_users() if item.id == user_id), None
        )
        if user is None:
            raise KeyError("user not found")
        principal = self.principals.find_by_field("user_id", user.id)
        if principal is None or principal.org_id != effective_org_id():
            raise KeyError("user principal not found")
        if user.status is not UserStatus.SUSPENDED:
            raise ValueError("only suspended users can be reactivated")
        timestamp = _now()
        active_user = User.model_validate(
            user.model_copy(
                update={"status": UserStatus.ACTIVE, "updated_at": timestamp}
            ).model_dump()
        )
        active_principal = Principal.model_validate(
            principal.model_copy(
                update={"status": PrincipalStatus.ACTIVE, "updated_at": timestamp}
            ).model_dump()
        )
        with self._unit_of_work():
            self.users.put(active_user)
            self.principals.put(active_principal)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=effective_workspace_id(),
                    actor_principal_id=requested_by_principal_id,
                    action="user.reactivated",
                    target_type="user",
                    target_id=user.id,
                    summary="Workspace user access restored",
                    request_id=user.id,
                    metadata={"principal_id": principal.id},
                    created_at=timestamp,
                )
            )
        return active_user
