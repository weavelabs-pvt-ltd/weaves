"""Workflow API route registrations migrated from the v0 API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ValidationError

from contracts.v1 import (
    WorkflowConditionOperator,
    WorkflowJoinPolicy,
    WorkflowParallelGroup,
    WorkflowStepCondition,
    WorkflowStepDependency,
)
from modules.workflows.definition_service import WorkflowDefinitionService
from modules.workflows.http.schemas import (
    WorkflowCreateRequest,
    WorkflowPatchRequest,
    WorkflowStepsReplaceRequest,
)

WorkflowDump = Callable[[BaseModel], dict[str, Any]]


def _workflow_trigger_view(trigger: BaseModel) -> dict[str, Any]:
    """Serialize a trigger without exposing the stored secret digest."""
    result = trigger.model_dump(mode="json")
    result.pop("secret_digest", None)
    result["has_secret"] = True
    return result


def _register_list_workflows(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the list workflows endpoint."""
    _dump = dump

    @app.get("/api/v0/workflows")
    def list_workflows() -> list[dict[str, Any]]:
        return [_dump(item) for item in definition_service.list_workflows()]


def _register_get_workflow(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the get workflow endpoint."""
    _dump = dump

    @app.get("/api/v0/workflows/{workflow_id}")
    def get_workflow(workflow_id: str) -> dict[str, Any]:
        try:
            workflow = definition_service.get_workflow(workflow_id)
            version = definition_service.get_current_workflow_version(workflow)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc
        return {
            "definition": _dump(workflow),
            "version": _dump(version),
            "graph": definition_service.graph_for_version(version),
        }


def _register_list_workflow_versions(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the list workflow versions endpoint."""
    _dump = dump

    @app.get("/api/v0/workflows/{workflow_id}/versions")
    def list_workflow_versions(
        workflow_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        before_version: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        try:
            versions = definition_service.list_workflow_versions(
                workflow_id, limit=limit + 1, before_version=before_version
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "items": [_dump(item) for item in versions[:limit]],
            "next_before_version": (
                versions[limit - 1].version if len(versions) > limit else None
            ),
        }


def _register_get_workflow_version(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the get workflow version endpoint."""
    _dump = dump

    @app.get("/api/v0/workflows/{workflow_id}/versions/{workflow_version_id}")
    def get_workflow_version(
        workflow_id: str, workflow_version_id: str
    ) -> dict[str, Any]:
        try:
            return _dump(
                definition_service.get_workflow_version(
                    workflow_id, workflow_version_id
                )
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Workflow version not found"
            ) from exc

    @app.get("/api/v0/workflows/{workflow_id}/versions/{workflow_version_id}/graph")
    def get_workflow_version_graph(
        workflow_id: str, workflow_version_id: str
    ) -> dict[str, Any]:
        try:
            version = definition_service.get_workflow_version(
                workflow_id, workflow_version_id
            )
            return {"graph": definition_service.graph_for_version(version)}
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Workflow version not found"
            ) from exc


def _register_create_workflow(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the create workflow endpoint."""
    _dump = dump

    @app.post("/api/v0/workflows", status_code=201)
    def create_workflow(request: WorkflowCreateRequest) -> dict[str, Any]:
        if (request.agent_id is None) == (request.agent_ids is None):
            raise HTTPException(
                status_code=422,
                detail="Provide exactly one of agent_id or agent_ids",
            )
        try:
            workflow = definition_service.create_workflow(**request.model_dump())
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        version = definition_service.get_current_workflow_version(workflow)
        return {
            "definition": _dump(workflow),
            "version": _dump(version),
            "graph": definition_service.graph_for_version(version),
        }


def _register_update_workflow(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the update workflow endpoint."""
    _dump = dump

    @app.patch("/api/v0/workflows/{workflow_id}")
    def update_workflow(
        workflow_id: str, patch: WorkflowPatchRequest
    ) -> dict[str, Any]:
        changes = patch.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status_code=400, detail="No changes supplied")
        try:
            return _dump(definition_service.update_workflow(workflow_id, **changes))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def _register_replace_workflow_steps(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register the replace workflow steps endpoint."""
    _dump = dump

    @app.put("/api/v0/workflows/{workflow_id}/steps")
    def replace_workflow_steps(
        workflow_id: str, request: WorkflowStepsReplaceRequest
    ) -> dict[str, Any]:
        try:
            condition_contracts: list[WorkflowStepCondition] = []
            for item in request.step_conditions:
                payload = item.model_dump(exclude_unset=True)
                if (
                    item.operator is WorkflowConditionOperator.EXISTS
                    and "expected_value" not in payload
                ):
                    payload["expected_value"] = None
                condition_contracts.append(
                    WorkflowStepCondition.model_validate(payload)
                )
            conditions = tuple(condition_contracts)
            parallel_groups = tuple(
                WorkflowParallelGroup.model_validate(item.model_dump())
                for item in request.parallel_groups
            )
            step_dependencies = tuple(
                WorkflowStepDependency(
                    target_agent_id=item.target_agent_id,
                    depends_on_agent_ids=item.depends_on_agent_ids,
                    join_policy=WorkflowJoinPolicy(item.join_policy),
                )
                for item in request.step_dependencies
            )
            workflow = definition_service.replace_workflow_agents(
                workflow_id,
                request.agent_ids,
                conditions,
                parallel_groups=parallel_groups,
                step_dependencies=step_dependencies,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Agent or workflow not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        version = definition_service.get_current_workflow_version(workflow)
        return {
            "definition": _dump(workflow),
            "version": _dump(version),
            "graph": definition_service.graph_for_version(version),
        }


def register_definition_routes(
    app: FastAPI,
    definition_service: WorkflowDefinitionService,
    dump: WorkflowDump,
) -> None:
    """Register definition routes."""
    _register_list_workflows(app, definition_service, dump)
    _register_get_workflow(app, definition_service, dump)
    _register_list_workflow_versions(app, definition_service, dump)
    _register_get_workflow_version(app, definition_service, dump)
    _register_create_workflow(app, definition_service, dump)
    _register_update_workflow(app, definition_service, dump)
    _register_replace_workflow_steps(app, definition_service, dump)
