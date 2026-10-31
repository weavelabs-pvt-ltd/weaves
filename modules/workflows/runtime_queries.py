"""Workflow definition lifecycle use cases."""

from __future__ import annotations

import base64
import json
from datetime import datetime

from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStatus,
    WorkflowVersion,
)
from modules.workflows.runtime_definition_host import WorkflowDefinitionRuntimeHost
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class WorkflowQueryMixin(WorkflowDefinitionRuntimeHost):
    """Read workflow versions and execution history."""

    def update_workflow(
        self,
        workflow_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: WorkflowStatus | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowDefinition:
        """Update workflow metadata or archive/reactivate its published version."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_MANAGE)
        current = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        next_name = name.strip() if name is not None else current.name
        if any(
            item.workflow_id != workflow_id
            and item.name.casefold() == next_name.casefold()
            for item in self.workflows.list_scoped(current.org_id, current.workspace_id)
        ):
            raise ValueError("workflow name already exists in this workspace")
        now = _now()
        updated = WorkflowDefinition.model_validate(
            current.model_copy(
                update={
                    "name": next_name,
                    "description": (
                        description.strip()
                        if description is not None
                        else current.description
                    ),
                    "status": status or current.status,
                    "updated_at": now,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.workflows.put(updated)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=current.org_id,
                    target_scope=AuditTargetScope.WORKSPACE,
                    workspace_id=current.workspace_id,
                    actor_principal_id=actor,
                    action="workflow.updated",
                    target_type="workflow",
                    target_id=workflow_id,
                    summary="Workflow settings updated",
                    request_id=workflow_id,
                    metadata={"status": updated.status.value},
                    created_at=now,
                )
            )
        return updated

    def list_workflow_runs(
        self,
        workflow_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[WorkflowRun, ...]:
        """Return a keyset-paginated run history for one scoped workflow."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_RUN)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        if not 1 <= limit <= 101:
            raise ValueError("workflow run limit must be between 1 and 101")
        cursor_position: tuple[datetime, str] | None = None
        if cursor is not None:
            if not cursor or len(cursor) > 1024:
                raise ValueError("workflow run cursor is invalid")
            try:
                encoded = cursor + "=" * (-len(cursor) % 4)
                cursor_payload = json.loads(
                    base64.b64decode(encoded, altchars=b"-_", validate=True)
                )
                if (
                    not isinstance(cursor_payload, dict)
                    or set(cursor_payload) != {"created_at", "run_id"}
                    or not isinstance(cursor_payload["created_at"], str)
                    or not isinstance(cursor_payload["run_id"], str)
                    or not cursor_payload["run_id"]
                ):
                    raise ValueError
                cursor_created_at = datetime.fromisoformat(
                    cursor_payload["created_at"].replace("Z", "+00:00")
                )
                if cursor_created_at.utcoffset() is None:
                    raise ValueError
                cursor_position = (cursor_created_at, cursor_payload["run_id"])
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError("workflow run cursor is invalid") from exc
        runs = [
            item
            for item in self.runs.list_scoped(workflow.org_id, workflow.workspace_id)
            if item.workflow_id == workflow_id
            and (
                cursor_position is None
                or (item.created_at, item.run_id) < cursor_position
            )
        ]
        runs.sort(key=lambda item: (item.created_at, item.run_id), reverse=True)
        return tuple(runs[:limit])

    def get_workflow_version(
        self,
        workflow_id: str,
        workflow_version_id: str,
        requested_by_principal_id: str = "local-developer",
    ) -> WorkflowVersion:
        """Return a historical version only when it belongs to the scoped workflow."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_RUN)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        version = self.workflow_versions.get_scoped(
            workflow_version_id, workflow.org_id, workflow.workspace_id
        )
        if version.workflow_id != workflow_id:
            raise KeyError("workflow version not found")
        return version

    def list_workflow_versions(
        self,
        workflow_id: str,
        *,
        limit: int = 50,
        before_version: int | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[WorkflowVersion, ...]:
        """Return a bounded, newest-first page of scoped workflow versions."""
        self._ensure_workflow_runtime()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize(actor, Permission.WORKFLOWS_RUN)
        workflow = self.workflows.get_scoped(
            workflow_id, effective_org_id(), effective_workspace_id()
        )
        if not 1 <= limit <= 101:
            raise ValueError("version page limit must be between 1 and 101")
        if before_version is not None and before_version < 1:
            raise ValueError("before_version must be positive")
        versions = [
            item
            for item in self.workflow_versions.list_scoped(
                workflow.org_id, workflow.workspace_id
            )
            if item.workflow_id == workflow_id
            and (before_version is None or item.version < before_version)
        ]
        versions.sort(key=lambda item: item.version, reverse=True)
        return tuple(versions[:limit])

    @staticmethod
    def workflow_run_cursor(run: WorkflowRun) -> str:
        """Return an opaque continuation token for a workflow run page."""
        payload = json.dumps(
            {"created_at": run.created_at.isoformat(), "run_id": run.run_id},
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
