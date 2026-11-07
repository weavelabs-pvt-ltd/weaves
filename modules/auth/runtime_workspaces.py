"""Workspace lifecycle and default workspace provisioning."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    ApprovalPolicy,
    AuditEvent,
    AuditTargetScope,
    Permission,
    RiskLevel,
    Workspace,
    WorkspaceStatus,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import effective_org_id, effective_principal_id


class WorkspaceRuntimeHost(ABC):
    """Dependencies for creating, updating, and querying workspaces."""

    _bootstrapped: Callable[[], bool]
    _advisory_transaction_lock: Callable[[str], None] | None
    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    workspaces: Repository[Workspace]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize defaults before workspace operations."""
        raise NotImplementedError

    @abstractmethod
    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Enforce an organization-scoped workspace permission."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for workspace and default-resource writes."""
        raise NotImplementedError


class WorkspaceRuntimeMixin(WorkspaceRuntimeHost):
    """Implement workspace lifecycle and initial capability provisioning."""

    def list_managed_workspaces(
        self, requested_by_principal_id: str = "local-developer"
    ) -> tuple[Workspace, ...]:
        """List all organization workspaces, including archived ones, for admins."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(
            requested_by_principal_id, Permission.WORKSPACES_ADMIN
        )
        return tuple(
            sorted(
                self.workspaces.list_scoped(effective_org_id()),
                key=lambda item: (item.created_at, item.id),
            )
        )

    def create_workspace(
        self,
        *,
        name: str,
        environment: str = "default",
        requested_by_principal_id: str = "local-developer",
    ) -> Workspace:
        """Create a workspace with its default approval policy."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(
            requested_by_principal_id, Permission.WORKSPACES_ADMIN
        )
        timestamp = _now()
        workspace = Workspace(
            id=_id(),
            org_id=effective_org_id(),
            name=name,
            environment=environment,
            status=WorkspaceStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        policy = self._build_workspace_defaults(workspace, timestamp)
        with self._unit_of_work():
            self.workspaces.create(workspace)
            self.approval_policies.create(policy)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=workspace.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=workspace.id,
                    actor_principal_id=requested_by_principal_id,
                    action="workspace.created",
                    target_type="workspace",
                    target_id=workspace.id,
                    summary="Workspace and default capabilities provisioned",
                    request_id=workspace.id,
                    metadata={"environment": environment},
                    created_at=timestamp,
                )
            )
        return workspace

    @staticmethod
    def _build_workspace_defaults(
        workspace: Workspace, timestamp: Any
    ) -> ApprovalPolicy:
        policy = ApprovalPolicy(
            approval_policy_id=_id(),
            org_id=workspace.org_id,
            workspace_id=workspace.id,
            name="Require review for medium and high risk actions",
            required_risk_levels=(RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL),
            allow_self_approval=False,
            enabled=True,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return policy

    def update_workspace(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        environment: str | None = None,
        status: WorkspaceStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> Workspace:
        """Rename, archive, or restore a workspace and audit the change."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(
            requested_by_principal_id, Permission.WORKSPACES_ADMIN
        )
        if name is None and environment is None and status is None:
            raise ValueError("provide at least one workspace change")
        org_id = effective_org_id()
        with self._unit_of_work():
            if self._advisory_transaction_lock is not None:
                self._advisory_transaction_lock(f"workspace-lifecycle:{org_id}")
            workspace = self.workspaces.get_scoped(workspace_id, org_id)
            changes: dict[str, Any] = {}
            if name is not None:
                changes["name"] = name
            if environment is not None:
                changes["environment"] = environment
            if status is not None:
                if (
                    status is WorkspaceStatus.ARCHIVED
                    and workspace.status is not status
                ):
                    active_workspaces = tuple(
                        item
                        for item in self.workspaces.list_scoped(org_id)
                        if item.status is WorkspaceStatus.ACTIVE
                        and item.id != workspace.id
                    )
                    if not active_workspaces:
                        raise ValueError(
                            "cannot archive the organization's last active workspace"
                        )
                changes["status"] = status
            if not changes:
                return workspace
            updated = Workspace.model_validate(
                workspace.model_copy(
                    update={**changes, "updated_at": _now()}
                ).model_dump()
            )
            self.workspaces.put(updated)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=updated.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=updated.id,
                    actor_principal_id=requested_by_principal_id,
                    action="workspace.updated",
                    target_type="workspace",
                    target_id=updated.id,
                    summary="Workspace settings or lifecycle status updated",
                    request_id=updated.id,
                    metadata={"changes": changes},
                    created_at=updated.updated_at,
                )
            )
        return updated
