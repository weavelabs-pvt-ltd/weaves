"""Workspace, role, and membership administration use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    ApprovalPolicy,
    AuditEvent,
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
from modules.auth.runtime_memberships import MembershipRuntimeMixin
from modules.auth.runtime_workspaces import WorkspaceRuntimeMixin


class WorkspaceAccessService(WorkspaceRuntimeMixin, MembershipRuntimeMixin):
    """Manage workspace defaults, roles, and membership using injected ports."""

    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    knowledge_documents: Repository[KnowledgeDocument]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    workspaces: Repository[Workspace]

    def __init__(
        self,
        *,
        approval_policies: Repository[ApprovalPolicy],
        audit_events: Repository[AuditEvent],
        installations: Repository[PluginInstallation],
        knowledge_documents: Repository[KnowledgeDocument],
        principals: Repository[Principal],
        role_bindings: Repository[RoleBinding],
        roles: Repository[Role],
        workspaces: Repository[Workspace],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        advisory_transaction_lock: Callable[[str], None] | None,
    ) -> None:
        """Bind workspace repositories and identity lifecycle operations."""
        self.approval_policies = approval_policies
        self.audit_events = audit_events
        self.installations = installations
        self.knowledge_documents = knowledge_documents
        self.principals = principals
        self.role_bindings = role_bindings
        self.roles = roles
        self.workspaces = workspaces
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._authorize_organization = authorize_organization
        self._open_unit_of_work = unit_of_work
        self._advisory_transaction_lock = advisory_transaction_lock

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Enforce workspace role permissions through the identity service."""
        self._authorize(principal_id, permission)

    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce organization role permissions through the identity service."""
        self._authorize_organization(principal_id, permission)

    def list_principal_workspaces(self, principal_id: str) -> tuple[Workspace, ...]:
        """List active workspaces visible through the principal's role bindings."""
        if not self._bootstrapped():
            self.bootstrap()
        principal = self.principals.get(principal_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise PermissionError("principal is not active")
        bindings = tuple(
            binding
            for binding in self.role_bindings.list_scoped(principal.org_id)
            if binding.principal_id == principal.id
        )
        workspaces = self.workspaces.list_scoped(principal.org_id)
        organization_wide = any(binding.workspace_id is None for binding in bindings)
        allowed_ids = {binding.workspace_id for binding in bindings}
        return tuple(
            sorted(
                (
                    workspace
                    for workspace in workspaces
                    if workspace.status is WorkspaceStatus.ACTIVE
                    and (organization_wide or workspace.id in allowed_ids)
                ),
                key=lambda workspace: workspace.id,
            )
        )

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for workspace and membership updates."""
        return self._open_unit_of_work()
