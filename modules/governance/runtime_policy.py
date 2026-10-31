"""Manage workspace approval policies and searchable audit history."""

from __future__ import annotations

import base64
import json
from datetime import datetime

from application.runtime_host import RuntimeCapabilityHost
from contracts.v1 import (
    ApprovalPolicy,
    AuditEvent,
    AuditTargetScope,
    Permission,
    RiskLevel,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class GovernancePolicyMixin(RuntimeCapabilityHost):
    """Own workspace approval-policy and audit-search operations."""

    def default_approval_policy_id(self) -> str:
        """Return an enabled approval policy in the current workspace."""
        policies = self.approval_policies.list_scoped(
            effective_org_id(), effective_workspace_id()
        )
        policy = next(
            (
                item
                for item in sorted(policies, key=lambda item: item.name)
                if item.enabled
            ),
            None,
        )
        if policy is None:
            raise ValueError("configure an approval policy before creating agents")
        return policy.approval_policy_id

    def list_approval_policies(self) -> tuple[ApprovalPolicy, ...]:
        """List approval policies in the selected workspace."""
        principal_id = effective_principal_id("local-developer")
        try:
            self.authorize(principal_id, Permission.ACTIONS_APPROVE)
        except PermissionError:
            self.authorize(principal_id, Permission.APPROVAL_POLICIES_MANAGE)
        return tuple(
            sorted(
                self.approval_policies.list_scoped(
                    effective_org_id(), effective_workspace_id()
                ),
                key=lambda item: item.name.casefold(),
            )
        )

    def get_approval_policy(self, policy_id: str) -> ApprovalPolicy:
        """Read one policy inside the selected workspace."""
        principal_id = effective_principal_id("local-developer")
        try:
            self.authorize(principal_id, Permission.ACTIONS_APPROVE)
        except PermissionError:
            self.authorize(principal_id, Permission.APPROVAL_POLICIES_MANAGE)
        return self.approval_policies.get_scoped(
            policy_id, effective_org_id(), effective_workspace_id()
        )

    def create_approval_policy(
        self,
        *,
        name: str,
        required_risk_levels: tuple[RiskLevel, ...],
        allow_self_approval: bool,
        enabled: bool = True,
        requested_by_principal_id: str = "local-developer",
    ) -> ApprovalPolicy:
        """Create a workspace policy for approval-backed actions."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.APPROVAL_POLICIES_MANAGE)
        if len(required_risk_levels) != len(set(required_risk_levels)):
            raise ValueError("required risk levels must be unique")
        timestamp = _now()
        policy = ApprovalPolicy(
            approval_policy_id=_id(),
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            name=name,
            required_risk_levels=required_risk_levels,
            allow_self_approval=allow_self_approval,
            enabled=enabled,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._unit_of_work():
            self.approval_policies.create(policy)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=policy.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=policy.workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="approval_policy.created",
                    target_type="approval_policy",
                    target_id=policy.approval_policy_id,
                    summary="Approval policy created",
                    request_id=policy.approval_policy_id,
                    metadata={
                        "required_risk_levels": [
                            level.value for level in required_risk_levels
                        ],
                        "allow_self_approval": allow_self_approval,
                        "enabled": enabled,
                    },
                    created_at=timestamp,
                )
            )
        return policy

    def update_approval_policy(
        self,
        policy_id: str,
        *,
        name: str | None = None,
        required_risk_levels: tuple[RiskLevel, ...] | None = None,
        allow_self_approval: bool | None = None,
        enabled: bool | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> ApprovalPolicy:
        """Update policy behavior immediately for pinned agent versions."""
        self._ensure_bootstrapped()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize(requested_by_principal_id, Permission.APPROVAL_POLICIES_MANAGE)
        current = self.approval_policies.get_scoped(
            policy_id, effective_org_id(), effective_workspace_id()
        )
        if (
            name is None
            and required_risk_levels is None
            and allow_self_approval is None
            and enabled is None
        ):
            raise ValueError("no approval policy changes supplied")
        levels = (
            required_risk_levels
            if required_risk_levels is not None
            else current.required_risk_levels
        )
        if len(levels) != len(set(levels)):
            raise ValueError("required risk levels must be unique")
        timestamp = _now()
        updated = ApprovalPolicy.model_validate(
            current.model_copy(
                update={
                    "name": name if name is not None else current.name,
                    "required_risk_levels": levels,
                    "allow_self_approval": (
                        allow_self_approval
                        if allow_self_approval is not None
                        else current.allow_self_approval
                    ),
                    "enabled": enabled if enabled is not None else current.enabled,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.approval_policies.put(updated)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=updated.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=updated.workspace_id,
                    actor_principal_id=requested_by_principal_id,
                    action="approval_policy.updated",
                    target_type="approval_policy",
                    target_id=updated.approval_policy_id,
                    summary="Approval policy updated",
                    request_id=updated.approval_policy_id,
                    metadata={
                        "required_risk_levels": [level.value for level in levels],
                        "allow_self_approval": updated.allow_self_approval,
                        "enabled": updated.enabled,
                    },
                    created_at=timestamp,
                )
            )
        return updated

    def search_audit_events(
        self,
        *,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        actor_principal_id: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[AuditEvent, ...]:
        """Search the selected workspace's audit stream with keyset pagination."""
        self.authorize(effective_principal_id("local-developer"), Permission.AUDIT_READ)
        if not 1 <= limit <= 1001:
            raise ValueError("limit must be between 1 and 1001")
        if created_after is not None and created_after.utcoffset() is None:
            raise ValueError("created_after must include a timezone")
        if created_before is not None and created_before.utcoffset() is None:
            raise ValueError("created_before must include a timezone")
        if (
            created_after is not None
            and created_before is not None
            and created_after > created_before
        ):
            raise ValueError("created_after must not be later than created_before")
        cursor_position: tuple[datetime, str] | None = None
        if cursor is not None:
            if not cursor or len(cursor) > 1024:
                raise ValueError("audit cursor is invalid")
            try:
                encoded = cursor + "=" * (-len(cursor) % 4)
                cursor_payload = json.loads(
                    base64.b64decode(encoded, altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(cursor_payload, dict)
                    or set(cursor_payload) != {"created_at", "event_id"}
                    or not isinstance(cursor_payload["created_at"], str)
                    or not isinstance(cursor_payload["event_id"], str)
                    or not cursor_payload["event_id"]
                ):
                    raise ValueError
                cursor_created_at = datetime.fromisoformat(
                    cursor_payload["created_at"].replace("Z", "+00:00")
                )
                if cursor_created_at.utcoffset() is None:
                    raise ValueError
                cursor_position = (
                    cursor_created_at,
                    cursor_payload["event_id"],
                )
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError("audit cursor is invalid") from exc
        return self.audit_events.list_audit_page(
            effective_org_id(),
            effective_workspace_id(),
            action=action,
            target_type=target_type,
            target_id=target_id,
            actor_principal_id=actor_principal_id,
            created_after=created_after,
            created_before=created_before,
            cursor_created_at=(cursor_position[0] if cursor_position else None),
            cursor_event_id=(cursor_position[1] if cursor_position else None),
            limit=limit,
        )

    @staticmethod
    def audit_event_cursor(event: AuditEvent) -> str:
        """Return an opaque continuation token for an audit event page."""
        payload = json.dumps(
            {
                "created_at": event.created_at.isoformat(),
                "event_id": event.audit_event_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
