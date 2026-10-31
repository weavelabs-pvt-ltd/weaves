"""Workspace and role-binding lifecycle use cases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    ApprovalPolicy,
    AuditEvent,
    AuditTargetScope,
    KnowledgeDocument,
    Permission,
    PluginInstallation,
    Principal,
    PrincipalStatus,
    Role,
    RoleBinding,
    Workspace,
    WorkspaceStatus,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class MembershipRuntimeHost(ABC):
    """Dependencies needed to manage workspaces, roles, and their membership."""

    _bootstrapped: Callable[[], bool]
    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    knowledge_documents: Repository[KnowledgeDocument]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    workspaces: Repository[Workspace]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before membership operations."""
        raise NotImplementedError

    @abstractmethod
    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce a workspace-scoped permission."""
        raise NotImplementedError

    @abstractmethod
    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce an organization-scoped permission."""
        raise NotImplementedError

    @abstractmethod
    def list_principal_workspaces(self, principal_id: str) -> tuple[Workspace, ...]:
        """List workspaces the active principal can access."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for related membership writes."""
        raise NotImplementedError


class MembershipRuntimeMixin(MembershipRuntimeHost):
    """Implement workspace lifecycle and role-based access bindings."""

    def create_role(
        self,
        *,
        name: str,
        permissions: tuple[Permission, ...],
        requested_by_principal_id: str = "local-developer",
    ) -> Role:
        """Create a reusable organization role with explicit permissions."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(requested_by_principal_id, Permission.ROLES_MANAGE)
        if not permissions:
            raise ValueError("role must grant at least one permission")
        if len(permissions) != len(set(permissions)):
            raise ValueError("role permissions must be unique")
        role = Role(
            id=_id(),
            org_id=effective_org_id(),
            name=name,
            permissions=list(permissions),
        )
        timestamp = _now()
        with self._unit_of_work():
            self.roles.create(role)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=requested_by_principal_id,
                    action="role.created",
                    target_type="role",
                    target_id=role.id,
                    summary="Organization role created",
                    request_id=role.id,
                    metadata={"permissions": [item.value for item in permissions]},
                    created_at=timestamp,
                )
            )
        return role

    def list_roles(self) -> tuple[Role, ...]:
        """List organization roles for an authorized role administrator."""
        self.authorize_organization(
            effective_principal_id("local-developer"), Permission.ROLES_MANAGE
        )
        return tuple(
            sorted(
                self.roles.list_scoped(effective_org_id()), key=lambda item: item.name
            )
        )

    def create_role_binding(
        self,
        *,
        principal_id: str,
        role_id: str,
        workspace_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> RoleBinding:
        """Assign an organization role to an active principal at explicit scope."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        org_id = self._authorize_role_binding_scope(
            requested_by_principal_id, workspace_id
        )
        principal = self.principals.get_scoped(principal_id, org_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise ValueError("role bindings require an active principal")
        self.roles.get_scoped(role_id, org_id)

        def find_existing_binding() -> RoleBinding | None:
            return next(
                (
                    item
                    for item in self.role_bindings.list_scoped(org_id)
                    if item.principal_id == principal_id
                    and item.role_id == role_id
                    and item.workspace_id == workspace_id
                ),
                None,
            )

        existing = find_existing_binding()
        if existing is not None:
            return existing
        timestamp = _now()
        binding = RoleBinding(
            id=_id(),
            org_id=org_id,
            principal_id=principal_id,
            role_id=role_id,
            workspace_id=workspace_id,
            created_at=timestamp,
        )
        try:
            with self._unit_of_work():
                existing = find_existing_binding()
                if existing is not None:
                    return existing
                self.role_bindings.create(binding)
                self.audit_events.create(
                    AuditEvent(
                        audit_event_id=_id(),
                        org_id=org_id,
                        target_scope=(
                            AuditTargetScope.ORGANIZATION
                            if workspace_id is None
                            else AuditTargetScope.WORKSPACE
                        ),
                        workspace_id=workspace_id,
                        actor_principal_id=requested_by_principal_id,
                        action="role_binding.created",
                        target_type="role_binding",
                        target_id=binding.id,
                        summary="Role assigned to principal",
                        request_id=binding.id,
                        metadata={"principal_id": principal_id, "role_id": role_id},
                        created_at=timestamp,
                    )
                )
        except ValueError:
            existing = find_existing_binding()
            if existing is None:
                raise
            return existing
        return binding

    def _authorize_role_binding_scope(
        self, actor: str, workspace_id: str | None
    ) -> str:
        org_id = effective_org_id()
        if workspace_id is None:
            self.authorize_organization(actor, Permission.ROLES_MANAGE)
            return org_id
        self.authorize(actor, Permission.ROLES_MANAGE)
        if workspace_id != effective_workspace_id():
            raise PermissionError("role binding must target the selected workspace")
        workspace = self.workspaces.get_scoped(workspace_id, org_id)
        if workspace.status is not WorkspaceStatus.ACTIVE:
            raise ValueError("role bindings require an active workspace")
        return org_id

    def list_role_bindings(self) -> tuple[RoleBinding, ...]:
        """List role assignments visible at the caller's authorization scope."""
        actor = effective_principal_id("local-developer")
        self.authorize(actor, Permission.ROLES_MANAGE)
        try:
            self.authorize_organization(actor, Permission.ROLES_MANAGE)
            can_view_organization_bindings = True
        except PermissionError:
            can_view_organization_bindings = False
        return tuple(
            sorted(
                (
                    item
                    for item in self.role_bindings.list_scoped(effective_org_id())
                    if item.workspace_id == effective_workspace_id()
                    or (can_view_organization_bindings and item.workspace_id is None)
                ),
                key=lambda item: item.id,
            )
        )

    def delete_role_binding(
        self,
        binding_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> None:
        """Remove a visible role assignment without orphaning org admins."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        org_id = effective_org_id()
        binding = self.role_bindings.get_scoped(binding_id, org_id)
        if binding.workspace_id is None:
            self.authorize_organization(
                requested_by_principal_id, Permission.ROLES_MANAGE
            )
        else:
            self.authorize(requested_by_principal_id, Permission.ROLES_MANAGE)
            if binding.workspace_id != effective_workspace_id():
                raise KeyError("role binding not found in requested scope")
        role = self.roles.get_scoped(binding.role_id, org_id)
        bindings = self.role_bindings.list_scoped(org_id)
        if binding.workspace_id is None:
            protected_permissions = {
                Permission.ORGANIZATIONS_ADMIN,
                Permission.ROLES_MANAGE,
            }.intersection(role.permissions)
            for permission in protected_permissions:
                another_org_admin = any(
                    candidate.id != binding.id
                    and candidate.workspace_id is None
                    and permission
                    in self.roles.get_scoped(candidate.role_id, org_id).permissions
                    for candidate in bindings
                )
                if not another_org_admin:
                    raise ValueError(
                        "cannot remove the last organization-level administrator"
                    )
        elif binding.principal_id == requested_by_principal_id and (
            Permission.ROLES_MANAGE in role.permissions
        ):
            another_workspace_admin = any(
                candidate.id != binding.id
                and candidate.principal_id == requested_by_principal_id
                and candidate.workspace_id == binding.workspace_id
                and Permission.ROLES_MANAGE
                in self.roles.get_scoped(candidate.role_id, org_id).permissions
                for candidate in bindings
            )
            if not another_workspace_admin:
                raise ValueError("cannot remove your last workspace role administrator")
        timestamp = _now()
        with self._unit_of_work():
            self.role_bindings.delete(binding.id)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=org_id,
                    target_scope=(
                        AuditTargetScope.ORGANIZATION
                        if binding.workspace_id is None
                        else AuditTargetScope.WORKSPACE
                    ),
                    workspace_id=binding.workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="role_binding.deleted",
                    target_type="role_binding",
                    target_id=binding.id,
                    summary="Role assignment removed",
                    request_id=binding.id,
                    metadata={
                        "principal_id": binding.principal_id,
                        "role_id": binding.role_id,
                    },
                    created_at=timestamp,
                )
            )
