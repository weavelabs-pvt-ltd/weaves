"""Governance use cases composed from repositories and runtime ports."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from threading import RLock
from typing import TYPE_CHECKING

from application.ports.repository import Repository
from contracts.v1 import (
    AgentRun,
    AgentVersion,
    ApprovalPolicy,
    ApprovalRequest,
    AuditEvent,
    CapabilityKind,
    CapabilitySpec,
    Permission,
    PluginInstallation,
    ToolInvocation,
)
from modules.governance.runtime_approvals import GovernanceApprovalMixin
from modules.governance.runtime_policy import GovernancePolicyMixin

if TYPE_CHECKING:
    from application.ports.runtime_adapters import PluginDispatcher

CapabilityInstallation = Callable[..., tuple[PluginInstallation, CapabilitySpec]]


class GovernanceService(GovernanceApprovalMixin, GovernancePolicyMixin):
    """Coordinate approval policies, proposals, decisions, and audit search."""

    approvals: Repository[ApprovalRequest]
    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    agent_runs: Repository[AgentRun]
    agent_versions: Repository[AgentVersion]
    tool_invocations: Repository[ToolInvocation]
    plugin_gateway: PluginDispatcher

    def __init__(
        self,
        *,
        approvals: Repository[ApprovalRequest],
        approval_policies: Repository[ApprovalPolicy],
        audit_events: Repository[AuditEvent],
        agent_runs: Repository[AgentRun],
        agent_versions: Repository[AgentVersion],
        tool_invocations: Repository[ToolInvocation],
        plugin_gateway: PluginDispatcher,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
        capability_installation: CapabilityInstallation,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        approval_lock: RLock,
    ) -> None:
        """Bind governance ports supplied by the process composition root."""
        self.approvals = approvals
        self.approval_policies = approval_policies
        self.audit_events = audit_events
        self.agent_runs = agent_runs
        self.agent_versions = agent_versions
        self.tool_invocations = tool_invocations
        self.plugin_gateway = plugin_gateway
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize
        self._resolve_capability_installation = capability_installation
        self._unit_of_work_impl = unit_of_work
        self._approval_lock = approval_lock

    def bootstrap(self) -> None:
        """Delegate bootstrap to the application lifecycle owner."""
        self._bootstrap()

    def _ensure_bootstrapped(self) -> None:
        """Bootstrap built-in records when the application has not done so."""
        if not self._is_bootstrapped():
            self.bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Delegate authorization to the configured identity capability."""
        self._authorize(principal_id, permission)

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open the transaction configured by the persistence composition root."""
        return self._unit_of_work_impl()

    def _capability_installation(
        self,
        capability_id: str,
        *,
        org_id: str,
        workspace_id: str,
        require_enabled: bool,
        allowed_kinds: tuple[CapabilityKind, ...] = (CapabilityKind.CONTEXT_READ,),
    ) -> tuple[PluginInstallation, CapabilitySpec]:
        """Resolve a capability through the integration capability service."""
        return self._resolve_capability_installation(
            capability_id,
            org_id=org_id,
            workspace_id=workspace_id,
            require_enabled=require_enabled,
            allowed_kinds=allowed_kinds,
        )
