"""Recover interrupted approved writes without retrying uncertain actions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta

from application.ports.repository import Repository
from application.runtime_constants import STALE_ACTION_INVOCATION_AFTER_SECONDS
from contracts.v1 import (
    ApprovalRequest,
    AuditEvent,
    AuditTargetScope,
    ErrorSummary,
    InvocationStatus,
    ToolInvocation,
)
from shared.clock import utc_now as _now


class ActionRecoveryService:
    """Mark stale write invocations uncertain and preserve an audit trail."""

    def __init__(
        self,
        *,
        tool_invocations: Repository[ToolInvocation],
        approvals: Repository[ApprovalRequest],
        audit_events: Repository[AuditEvent],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind write invocation repositories and transaction boundaries."""
        self._tool_invocations = tool_invocations
        self._approvals = approvals
        self._audit_events = audit_events
        self._open_unit_of_work = unit_of_work

    def recover_stale_action_invocations(
        self,
        *,
        stale_after_seconds: int = STALE_ACTION_INVOCATION_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Time out interrupted approved writes without attempting them again."""
        if stale_after_seconds < 1:
            raise ValueError("stale_after_seconds must be positive")
        timestamp = now or _now()
        stale_before = timestamp - timedelta(seconds=stale_after_seconds)
        recovered: list[str] = []
        for invocation in self._tool_invocations.list():
            if (
                invocation.status is not InvocationStatus.EXECUTING
                or invocation.updated_at > stale_before
            ):
                continue
            audit = self._recover_invocation(invocation, timestamp, stale_before)
            if audit:
                recovered.append(invocation.invocation_id)
        return tuple(recovered)

    def _recover_invocation(
        self, invocation: ToolInvocation, timestamp: datetime, stale_before: datetime
    ) -> bool:
        """Persist one timeout and audit event only if the lease is still stale."""
        duration_ms = min(
            86_400_000,
            max(
                0,
                int(
                    (
                        timestamp - (invocation.started_at or invocation.created_at)
                    ).total_seconds()
                    * 1000
                ),
            ),
        )
        error = ErrorSummary(
            code="action.interrupted",
            summary=(
                "The action worker stopped before recording a result. "
                "The external outcome is unknown; verify the target system "
                "before attempting the action again."
            ),
            retryable=False,
        )
        timed_out = ToolInvocation.model_validate(
            invocation.model_copy(
                update={
                    "status": InvocationStatus.TIMED_OUT,
                    "finished_at": timestamp,
                    "updated_at": timestamp,
                    "duration_ms": duration_ms,
                    "error": error,
                }
            ).model_dump()
        )
        with self._open_unit_of_work():
            if not self._tool_invocations.put_if_status_and_stale(
                timed_out,
                expected_status=InvocationStatus.EXECUTING.value,
                updated_before=stale_before,
            ):
                return False
            self._audit_events.put(
                self._interruption_audit(invocation, error, timestamp)
            )
        return True

    def _interruption_audit(
        self, invocation: ToolInvocation, error: ErrorSummary, timestamp: datetime
    ) -> AuditEvent:
        """Build an audit record for an externally uncertain action result."""
        approval = None
        if invocation.approval_request_id is not None:
            try:
                approval = self._approvals.get_scoped(
                    invocation.approval_request_id,
                    invocation.org_id,
                    invocation.workspace_id,
                )
            except KeyError:
                approval = None
        actor = (
            approval.resolved_by_principal_id
            if approval is not None and approval.resolved_by_principal_id
            else approval.requested_by_principal_id
            if approval is not None
            else "system"
        )
        return AuditEvent(
            audit_event_id=(
                "action-recovery-"
                + hashlib.sha256(invocation.invocation_id.encode()).hexdigest()[:32]
            ),
            org_id=invocation.org_id,
            target_scope=AuditTargetScope.WORKSPACE,
            workspace_id=invocation.workspace_id,
            actor_principal_id=actor,
            action="action.execution_interrupted",
            target_type="tool_invocation",
            target_id=invocation.invocation_id,
            summary=error.summary,
            request_id=invocation.run_id,
            metadata={"outcome_unknown": True, "retryable": False},
            created_at=timestamp,
        )
