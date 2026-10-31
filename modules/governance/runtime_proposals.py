"""Propose scoped write actions for human approval."""

from __future__ import annotations

from datetime import datetime

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    AgentRun,
    AgentVersion,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    AuditTargetScope,
    CapabilityKind,
    CapabilitySpec,
    InvocationStatus,
    Permission,
    PluginInstallation,
    RiskLevel,
    RunStatus,
    ToolInvocation,
    WorkflowRun,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class GovernanceProposalMixin(RuntimeCapabilityHost):
    """Validate agent authority and persist approved-action proposals."""

    def propose_jira_comment(
        self,
        run_id: str,
        *,
        issue_key: str,
        comment: str,
        agent_run_id: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[ApprovalRequest, ToolInvocation]:
        """Create a pending approval for a Jira comment; do not send it yet."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.AGENTS_RUN)
        run = self.runs.get_scoped(run_id, effective_org_id(), effective_workspace_id())
        if run.status is not RunStatus.SUCCEEDED:
            raise ValueError("actions can only be proposed for a completed run")
        agent_run, agent_version = self._resolve_jira_proposal_agent(
            run_id, agent_run_id
        )
        capability_id = "jira.issues.comment"
        installation, capability = self._validate_jira_proposal_policy(
            capability_id, agent_version
        )
        payload = {"issue_key": issue_key, "comment": comment}
        self.plugin_gateway.validate_approved_action_request(
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            installation_id=installation.plugin_installation_id,
            agent_version=agent_version,
            capability_id=capability_id,
            payload=payload,
        )
        now = _now()
        approval_id = _id()
        invocation = ToolInvocation(
            invocation_id=_id(),
            run_id=run_id,
            agent_run_id=agent_run.agent_run_id,
            org_id=run.org_id,
            workspace_id=run.workspace_id,
            plugin_installation_id=installation.plugin_installation_id,
            capability_id=capability_id,
            status=InvocationStatus.AWAITING_APPROVAL,
            risk_level=capability.risk_level or RiskLevel.MEDIUM,
            approval_request_id=approval_id,
            input_summary=payload,
            created_at=now,
            updated_at=now,
        )
        approval = ApprovalRequest(
            approval_request_id=approval_id,
            org_id=run.org_id,
            workspace_id=run.workspace_id,
            run_id=run_id,
            requested_by_agent_run_id=agent_run.agent_run_id,
            requested_by_principal_id=requested_by_principal_id,
            capability_id=capability_id,
            action_summary=f"Add a comment to Jira issue {issue_key}",
            risk_level=capability.risk_level or RiskLevel.MEDIUM,
            status=ApprovalStatus.PENDING,
            requested_at=now,
            created_at=now,
            updated_at=now,
        )
        self._persist_jira_approval_proposal(
            run, requested_by_principal_id, approval, invocation, now
        )
        return approval, invocation

    def _persist_jira_approval_proposal(
        self,
        run: WorkflowRun,
        actor: str,
        approval: ApprovalRequest,
        invocation: ToolInvocation,
        now: datetime,
    ) -> None:
        """Store approval, invocation, and audit event in one transaction."""
        with self._unit_of_work():
            self.approvals.create(approval)
            self.tool_invocations.create(invocation)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=run.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=run.workspace_id,
                    actor_principal_id=actor,
                    action="action.approval_requested",
                    target_type="approval_request",
                    target_id=approval.approval_request_id,
                    summary=approval.action_summary,
                    request_id=run.run_id,
                    metadata={
                        "capability_id": approval.capability_id,
                        "invocation_id": invocation.invocation_id,
                    },
                    created_at=now,
                )
            )

    def _resolve_jira_proposal_agent(
        self, run_id: str, requested_agent_run_id: str | None
    ) -> tuple[AgentRun, AgentVersion]:
        """Select the single workflow step allowed to propose a Jira comment."""
        candidates = tuple(
            item
            for item in self.agent_runs.list_scoped(
                effective_org_id(), effective_workspace_id()
            )
            if item.run_id == run_id
            and "jira.issues.comment"
            in self.agent_versions.get_scoped(
                item.agent_version_id, effective_org_id(), effective_workspace_id()
            ).allowed_capability_ids
            and (
                requested_agent_run_id is None
                or item.agent_run_id == requested_agent_run_id
            )
        )
        if len(candidates) != 1:
            if not candidates:
                raise ValueError("no workflow step is allowed to propose Jira comments")
            raise ValueError("agent_run_id is required to choose a workflow step")
        agent_run = candidates[0]
        version = self.agent_versions.get_scoped(
            agent_run.agent_version_id, effective_org_id(), effective_workspace_id()
        )
        return agent_run, version

    def _validate_jira_proposal_policy(
        self, capability_id: str, agent_version: AgentVersion
    ) -> tuple[PluginInstallation, CapabilitySpec]:
        """Validate installation and workspace policy for a proposed Jira action."""
        installation, capability = (
            self.integration_admin_service.resolve_capability_installation(
                capability_id,
                org_id=effective_org_id(),
                workspace_id=effective_workspace_id(),
                require_enabled=True,
                allowed_kinds=(CapabilityKind.ACTION_WRITE,),
            )
        )
        if not agent_version.approval_policy_id:
            raise ValueError("agent version has no approval policy")
        policy = self.approval_policies.get_scoped(
            agent_version.approval_policy_id,
            effective_org_id(),
            effective_workspace_id(),
        )
        if (
            not policy.enabled
            or capability.risk_level not in policy.required_risk_levels
        ):
            raise ValueError("workspace policy does not allow this action")
        return installation, capability
