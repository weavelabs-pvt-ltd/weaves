"""Decide approvals and execute validated actions with durable audit."""

from __future__ import annotations

from datetime import datetime

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentVersion,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    AuditTargetScope,
    CapabilityKind,
    ErrorSummary,
    InvocationStatus,
    Permission,
    PluginInstallation,
    ToolInvocation,
)
from contracts.v1.json_data import thaw_json_value
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.errors import PluginGatewayError
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class GovernanceApprovalMixin(RuntimeCapabilityHost):
    """Record approval decisions and guard write-action execution."""

    def decide_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decision_comment: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[ApprovalRequest, ToolInvocation]:
        """Record an approval decision and execute any approved action."""
        self._ensure_bootstrapped()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.ACTIONS_APPROVE)
        with self._approval_lock:
            approval, invocation = self._load_pending_approval(approval_id)
            prepared = (
                self._prepare_approved_action(approval, invocation, actor)
                if approved
                else None
            )
            now = _now()
            resolved, decision_audit = self._build_approval_decision(
                approval, actor, approved, decision_comment, now
            )
            if not approved:
                return resolved, self._reject_approval(
                    approval, invocation, resolved, decision_audit, now
                )
            if prepared is None:
                raise RuntimeError("approved action was not prepared")
            agent_version, installation = prepared
            return resolved, self._execute_approved_action(
                approval,
                invocation,
                resolved,
                decision_audit,
                agent_version,
                installation,
                actor,
                now,
            )

    def _load_pending_approval(
        self, approval_id: str
    ) -> tuple[ApprovalRequest, ToolInvocation]:
        """Load one unresolved approval and its unique pending invocation."""
        approval = self.approvals.get_scoped(
            approval_id, effective_org_id(), effective_workspace_id()
        )
        if approval.status is not ApprovalStatus.PENDING:
            raise ValueError("approval request is already resolved")
        invocations = tuple(
            item
            for item in self.tool_invocations.list_scoped(
                effective_org_id(), effective_workspace_id()
            )
            if item.approval_request_id == approval_id
        )
        if len(invocations) != 1:
            raise ValueError("approval request has no unique pending action")
        return approval, invocations[0]

    def _prepare_approved_action(
        self, approval: ApprovalRequest, invocation: ToolInvocation, actor: str
    ) -> tuple[AgentVersion, PluginInstallation]:
        """Recheck principal, policy, agent, and capability before approval."""
        if not invocation.agent_run_id:
            raise ValueError("approved action has no associated agent run")
        agent_run = self.agent_runs.get_scoped(
            invocation.agent_run_id, approval.org_id, approval.workspace_id
        )
        agent_version = self.agent_versions.get_scoped(
            agent_run.agent_version_id, approval.org_id, approval.workspace_id
        )
        policy_id = agent_version.approval_policy_id
        if not policy_id:
            raise ValueError("agent version has no approval policy")
        policy = self.approval_policies.get_scoped(
            policy_id, approval.org_id, approval.workspace_id
        )
        if not policy.enabled:
            raise ValueError("approval policy is disabled")
        if (
            actor == approval.requested_by_principal_id
            and not policy.allow_self_approval
        ):
            raise PermissionError("approval policy forbids self-approval")
        installation, _ = self._capability_installation(
            approval.capability_id,
            org_id=approval.org_id,
            workspace_id=approval.workspace_id,
            require_enabled=True,
            allowed_kinds=(CapabilityKind.ACTION_WRITE,),
        )
        return agent_version, installation

    @staticmethod
    def _build_approval_decision(
        approval: ApprovalRequest,
        actor: str,
        approved: bool,
        comment: str | None,
        now: datetime,
    ) -> tuple[ApprovalRequest, AuditEvent]:
        """Build resolved approval and decision audit records."""
        action = "action.approved" if approved else "action.rejected"
        resolved = ApprovalRequest.model_validate(
            approval.model_copy(
                update={
                    "status": ApprovalStatus.APPROVED
                    if approved
                    else ApprovalStatus.REJECTED,
                    "resolved_by_principal_id": actor,
                    "resolved_at": now,
                    "decision_comment": comment,
                    "updated_at": now,
                }
            ).model_dump()
        )
        audit = AuditEvent(
            audit_event_id=_id(),
            org_id=approval.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=approval.workspace_id,
            actor_principal_id=actor,
            action=action,
            target_type="approval_request",
            target_id=approval.approval_request_id,
            summary=comment or action,
            request_id=approval.run_id,
            metadata={"capability_id": approval.capability_id},
            created_at=now,
        )
        return resolved, audit

    def _reject_approval(
        self,
        approval: ApprovalRequest,
        invocation: ToolInvocation,
        resolved: ApprovalRequest,
        decision_audit: AuditEvent,
        now: datetime,
    ) -> ToolInvocation:
        """Persist a denial without invoking the external integration."""
        denied = ToolInvocation.model_validate(
            invocation.model_copy(
                update={
                    "status": InvocationStatus.DENIED,
                    "finished_at": now,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self._put_pending_approval(approval, resolved)
            self.audit_events.create(decision_audit)
            self.tool_invocations.put(denied)
        return denied

    def _put_pending_approval(
        self, approval: ApprovalRequest, resolved: ApprovalRequest
    ) -> None:
        """Update the approval only if it remains pending at its loaded version."""
        if not self.approvals.put_if_status_and_stale(
            resolved,
            expected_status=ApprovalStatus.PENDING.value,
            updated_before=approval.updated_at,
        ):
            raise ValueError("approval request is already resolved")

    def _execute_approved_action(
        self,
        approval: ApprovalRequest,
        invocation: ToolInvocation,
        resolved: ApprovalRequest,
        decision_audit: AuditEvent,
        agent_version: AgentVersion,
        installation: PluginInstallation,
        actor: str,
        now: datetime,
    ) -> ToolInvocation:
        """Persist the decision, invoke the integration, and store its result."""
        started, executing = self._start_approved_action(
            approval, invocation, resolved, decision_audit, now
        )
        try:
            result = self.plugin_gateway.invoke(
                org_id=approval.org_id,
                workspace_id=approval.workspace_id,
                installation_id=installation.plugin_installation_id,
                agent_version=agent_version,
                capability_id=approval.capability_id,
                payload=thaw_json_value(invocation.input_summary),
                approval_verified=True,
            )
        except PluginGatewayError as exc:
            self._fail_approved_action(approval, executing, actor, exc, started)
            raise
        succeeded = ToolInvocation.model_validate(
            executing.model_copy(
                update={
                    "status": InvocationStatus.SUCCEEDED,
                    "output_summary": result.output,
                    "finished_at": result.finished_at,
                    "updated_at": result.finished_at,
                    "duration_ms": result.duration_ms,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.tool_invocations.put(succeeded)
            self._audit_action_execution(
                approval,
                actor,
                "action.execution_succeeded",
                "Approved action completed",
            )
        return succeeded

    def _start_approved_action(
        self,
        approval: ApprovalRequest,
        invocation: ToolInvocation,
        resolved: ApprovalRequest,
        decision_audit: AuditEvent,
        now: datetime,
    ) -> tuple[datetime, ToolInvocation]:
        """Record approval and mark the invocation executing before network IO."""
        started = _now()
        executing = ToolInvocation.model_validate(
            invocation.model_copy(
                update={
                    "status": InvocationStatus.EXECUTING,
                    "started_at": started,
                    "updated_at": started,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self._put_pending_approval(approval, resolved)
            self.audit_events.create(decision_audit)
            self.tool_invocations.put(executing)
        return started, executing

    def _fail_approved_action(
        self,
        approval: ApprovalRequest,
        executing: ToolInvocation,
        actor: str,
        error: PluginGatewayError,
        started: datetime,
    ) -> None:
        """Store a failed integration invocation and action failure audit."""
        finished = _now()
        failed = ToolInvocation.model_validate(
            executing.model_copy(
                update={
                    "status": InvocationStatus.FAILED,
                    "finished_at": finished,
                    "updated_at": finished,
                    "duration_ms": max(
                        0, int((finished - started).total_seconds() * 1000)
                    ),
                    "error": ErrorSummary(code=error.code, summary=error.message),
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.tool_invocations.put(failed)
            self._audit_action_execution(
                approval, actor, "action.execution_failed", error.message
            )

    def _audit_action_execution(
        self,
        approval: ApprovalRequest,
        actor_principal_id: str,
        action: str,
        summary: str,
    ) -> None:
        self.audit_events.create(
            AuditEvent(
                audit_event_id=_id(),
                org_id=approval.org_id,
                target_scope=AuditTargetScope.WORKSPACE,
                workspace_id=approval.workspace_id,
                actor_principal_id=actor_principal_id,
                action=action,
                target_type="approval_request",
                target_id=approval.approval_request_id,
                summary=summary,
                request_id=approval.run_id,
                metadata={"capability_id": approval.capability_id},
                created_at=_now(),
            )
        )
