"""Approval policy and safe-write governance API routes."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from contracts.v1 import ApprovalStatus, Permission, RiskLevel
from modules.governance.service import GovernanceService
from shared.errors import PluginGatewayError
from shared.request_context import (
    effective_org_id,
    effective_workspace_id,
)


class ApprovalPolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    required_risk_levels: tuple[RiskLevel, ...] = (
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
        RiskLevel.CRITICAL,
    )
    allow_self_approval: bool = False
    enabled: bool = True


class ApprovalPolicyPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    required_risk_levels: tuple[RiskLevel, ...] | None = None
    allow_self_approval: bool | None = None
    enabled: bool | None = None


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    approved: bool
    decision_comment: str | None = Field(default=None, max_length=1000)


def _approval_view(
    governance_service: GovernanceService,
    approval: Any,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> dict[str, Any]:
    result = dump(approval)
    result["tool_invocations"] = [
        dump(item)
        for item in governance_service.tool_invocations.list_scoped(
            approval.org_id, approval.workspace_id
        )
        if item.approval_request_id == approval.approval_request_id
    ]
    return result


def register_governance_routes(
    app: FastAPI,
    governance_service: GovernanceService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register approval policies, proposals, and reviewer decisions."""
    _register_governance_routes_group_1(app, governance_service, dump)
    _register_governance_routes_group_2(app, governance_service, dump)


def _register_governance_routes_group_1(
    app: FastAPI,
    governance_service: GovernanceService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register approval policies, proposals, and reviewer decisions."""
    _dump = dump

    def list_approval_policies() -> list[dict[str, Any]]:
        try:
            return [_dump(item) for item in governance_service.list_approval_policies()]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def get_approval_policy(policy_id: str) -> dict[str, Any]:
        try:
            return _dump(governance_service.get_approval_policy(policy_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Approval policy not found"
            ) from exc

    def create_approval_policy(
        request: ApprovalPolicyCreateRequest,
    ) -> dict[str, Any]:
        try:
            policy = governance_service.create_approval_policy(**request.model_dump())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(policy)

    def update_approval_policy(
        policy_id: str, patch: ApprovalPolicyPatchRequest
    ) -> dict[str, Any]:
        changes = patch.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status_code=400, detail="No changes supplied")
        try:
            policy = governance_service.update_approval_policy(policy_id, **changes)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Approval policy not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(policy)

    def list_approvals(
        status: ApprovalStatus | None = None,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        governance_service.authorize("local-developer", Permission.ACTIONS_APPROVE)
        approvals = governance_service.approvals.list_scoped(
            effective_org_id(), effective_workspace_id()
        )
        if status is not None:
            approvals = tuple(item for item in approvals if item.status is status)
        ordered = sorted(approvals, key=lambda item: item.requested_at, reverse=True)[
            :limit
        ]
        return [_approval_view(governance_service, item, _dump) for item in ordered]

    app.get("/api/v0/approval-policies")(list_approval_policies)
    app.get("/api/v0/approval-policies/{policy_id}")(get_approval_policy)
    app.post("/api/v0/approval-policies", status_code=201)(create_approval_policy)
    app.patch("/api/v0/approval-policies/{policy_id}")(update_approval_policy)
    app.get("/api/v0/approvals")(list_approvals)


def _register_governance_routes_group_2(
    app: FastAPI,
    governance_service: GovernanceService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register approval policies, proposals, and reviewer decisions."""
    _dump = dump

    def decide_approval(
        approval_id: str, request: ApprovalDecisionRequest
    ) -> dict[str, Any]:
        try:
            approval, invocation = governance_service.decide_approval(
                approval_id,
                approved=request.approved,
                decision_comment=request.decision_comment,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Approval request not found"
            ) from exc
        except PluginGatewayError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except ValueError as exc:
            status_code = 409 if "already resolved" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {"approval": _dump(approval), "tool_invocation": _dump(invocation)}

    app.post("/api/v0/approvals/{approval_id}/decision")(decide_approval)
