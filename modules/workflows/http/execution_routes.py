"""Workflow API route registrations migrated from the v0 API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from application import (
    AgentRuntimeBudgetExceeded,
    IdempotencyConflict,
    RunCostBudgetExceeded,
)
from modules.workflows.execution_service import WorkflowExecutionService
from modules.workflows.http.schemas import (
    WorkflowRunCreateRequest,
)
from shared.errors import ModelGatewayError, PluginGatewayError

WorkflowDump = Callable[[BaseModel], dict[str, Any]]


def _workflow_trigger_view(trigger: BaseModel) -> dict[str, Any]:
    """Serialize a trigger without exposing the stored secret digest."""
    result = trigger.model_dump(mode="json")
    result.pop("secret_digest", None)
    result["has_secret"] = True
    return result


def _register_create_workflow_run(
    app: FastAPI,
    workflow_execution_service: WorkflowExecutionService,
    dump: WorkflowDump,
    run_view: Any,
) -> None:
    """Register the create workflow run endpoint."""
    _dump = dump
    _run_view = run_view

    @app.post("/api/v0/workflows/{workflow_id}/runs", status_code=201)
    def create_workflow_run(
        workflow_id: str,
        request: WorkflowRunCreateRequest,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ) -> dict[str, Any]:
        try:
            result = workflow_execution_service.run_workflow(
                workflow_id=workflow_id,
                task=request.task,
                thread_id=request.thread_id,
                idempotency_key=idempotency_key,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc
        except PluginGatewayError as exc:
            raise HTTPException(status_code=422, detail=exc.message) from exc
        except ModelGatewayError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except RunCostBudgetExceeded as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AgentRuntimeBudgetExceeded as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "run": _dump(result.run),
            "agent_run": _dump(result.agent_run),
            "agent_runs": [
                _dump(item)
                for item in (result.workflow_agent_runs or (result.agent_run,))
            ],
            "tool_invocations": [_dump(item) for item in result.tool_invocations],
            "artifacts": [
                _dump(item)
                for item in (result.workflow_artifacts or (result.artifact,))
            ],
            "audit_event": _dump(result.audit_event),
            "replayed": result.replayed,
        }


def register_execution_routes(
    app: FastAPI,
    workflow_execution_service: WorkflowExecutionService,
    dump: WorkflowDump,
    run_view: Any,
) -> None:
    """Register execution routes."""
    _register_create_workflow_run(app, workflow_execution_service, dump, run_view)
