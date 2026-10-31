"""Agent run, durable job, artifact history, and audit API routes."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from modules.agents.execution_service import AgentExecutionService
from modules.agents.runtime_state import (
    AgentRuntimeBudgetExceeded,
    RunCostBudgetExceeded,
)
from modules.executions.service import ExecutionService
from modules.governance.service import GovernanceService
from shared.errors import IdempotencyConflict, ModelGatewayError, PluginGatewayError


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task: str = Field(min_length=3, max_length=12000)
    agent_id: str | None = Field(default=None, max_length=128)
    thread_id: str | None = Field(default=None, max_length=128)


class ExecutionJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task: str = Field(min_length=3, max_length=12000)
    agent_id: str | None = Field(default=None, max_length=128)
    workflow_id: str | None = Field(default=None, max_length=128)
    thread_id: str | None = Field(default=None, max_length=128)


@dataclass(frozen=True)
class ExecutionRouteServices:
    """Hold the use cases required by execution HTTP endpoints."""

    agent_execution: AgentExecutionService
    execution: ExecutionService
    governance: GovernanceService
    durable_execution_enabled: bool


def register_execution_routes(
    app: FastAPI,
    services: ExecutionRouteServices,
    dump: Callable[[BaseModel], dict[str, Any]],
    run_view: Callable[[Any], dict[str, Any]],
) -> None:
    """Register agent execution, run inspection, and audit endpoints."""
    _register_execution_routes_group_1(app, services, dump)
    _register_execution_routes_group_2(app, services, dump)
    _register_execution_routes_group_3(app, services, dump, run_view)
    _register_execution_routes_group_4(app, services, dump)
    _register_execution_routes_group_5(app, services, dump)


def _register_execution_routes_group_1(
    app: FastAPI,
    services: ExecutionRouteServices,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register agent execution, run inspection, and audit endpoints."""
    _dump = dump

    def create_run(
        request: RunCreateRequest,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ) -> dict[str, Any]:
        try:
            result = services.agent_execution.run_agent(
                task=request.task,
                agent_id=request.agent_id,
                thread_id=request.thread_id,
                idempotency_key=idempotency_key,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Agent, model profile, or conversation thread not found",
            ) from exc
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

    app.post("/api/v0/runs", status_code=201)(create_run)


def _register_execution_routes_group_2(
    app: FastAPI,
    services: ExecutionRouteServices,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register agent execution, run inspection, and audit endpoints."""
    _dump = dump

    def enqueue_execution_job(
        request: ExecutionJobCreateRequest,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ) -> dict[str, Any]:
        if request.agent_id is not None and request.workflow_id is not None:
            raise HTTPException(
                status_code=422, detail="Choose either agent_id or workflow_id"
            )
        if not services.durable_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail="Durable execution jobs require PostgreSQL-backed storage",
            )
        try:
            job, replayed = services.execution.enqueue_agent_run(
                task=request.task,
                agent_id=request.agent_id,
                thread_id=request.thread_id,
                idempotency_key=idempotency_key,
                workflow_id=request.workflow_id,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Agent, workflow, or thread not found"
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job": _dump(job), "replayed": replayed}

    def list_execution_jobs(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        return [
            _dump(item) for item in services.execution.list_execution_jobs(limit=limit)
        ]

    def get_execution_job(job_id: str) -> dict[str, Any]:
        try:
            return _dump(services.execution.get_execution_job(job_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Execution job not found"
            ) from exc

    def cancel_execution_job(job_id: str) -> dict[str, Any]:
        try:
            return _dump(services.execution.cancel_execution_job(job_id))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Execution job not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.post("/api/v0/execution-jobs", status_code=202)(enqueue_execution_job)
    app.get("/api/v0/execution-jobs")(list_execution_jobs)
    app.get("/api/v0/execution-jobs/{job_id}")(get_execution_job)
    app.post("/api/v0/execution-jobs/{job_id}/cancel")(cancel_execution_job)


def _register_execution_routes_group_3(
    app: FastAPI,
    services: ExecutionRouteServices,
    dump: Callable[[BaseModel], dict[str, Any]],
    run_view: Callable[[Any], dict[str, Any]],
) -> None:
    """Register agent execution, run inspection, and audit endpoints."""
    _dump = dump

    def retry_execution_job(
        job_id: str,
        idempotency_key: str = Header(
            alias="Idempotency-Key", min_length=1, max_length=128
        ),
    ) -> dict[str, Any]:
        if not services.durable_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Durable execution job retries require PostgreSQL-backed storage"
                ),
            )
        try:
            job, replayed = services.execution.retry_execution_job(
                job_id, idempotency_key=idempotency_key
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Execution job not found"
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"job": _dump(job), "replayed": replayed}

    def list_artifacts(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        return [
            _dump(item) for item in services.agent_execution.list_artifacts(limit=limit)
        ]

    def list_runs(limit: int = Query(default=50, ge=1, le=100)) -> list[dict[str, Any]]:
        runs = services.agent_execution.list_runs(limit=limit)
        return [run_view(item) for item in runs]

    def get_run(run_id: str) -> dict[str, Any]:
        try:
            run = services.agent_execution.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return run_view(run)

    app.post("/api/v0/execution-jobs/{job_id}/retry", status_code=202)(
        retry_execution_job
    )
    app.get("/api/v0/artifacts")(list_artifacts)
    app.get("/api/v0/runs")(list_runs)
    app.get("/api/v0/runs/{run_id}")(get_run)


def _register_execution_routes_group_4(
    app: FastAPI,
    services: ExecutionRouteServices,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register agent execution, run inspection, and audit endpoints."""
    _dump = dump

    def list_run_artifacts(run_id: str) -> list[dict[str, Any]]:
        try:
            artifacts = services.agent_execution.list_run_artifacts(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return [_dump(item) for item in artifacts]

    def list_audit_events(
        limit: int = Query(default=50, ge=1, le=100),
        action: str | None = Query(default=None, min_length=1, max_length=128),
        target_type: str | None = Query(default=None, min_length=1, max_length=128),
        target_id: str | None = Query(default=None, min_length=1, max_length=128),
        actor_principal_id: str | None = Query(
            default=None, min_length=1, max_length=128
        ),
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        try:
            events = services.governance.search_audit_events(
                action=action,
                target_type=target_type,
                target_id=target_id,
                actor_principal_id=actor_principal_id,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return [_dump(item) for item in events]

    app.get("/api/v0/runs/{run_id}/artifacts")(list_run_artifacts)
    app.get("/api/v0/audit-events")(list_audit_events)


def _register_execution_routes_group_5(
    app: FastAPI,
    services: ExecutionRouteServices,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register agent execution, run inspection, and audit endpoints."""
    _dump = dump

    def export_audit_events(
        limit: int = Query(default=500, ge=1, le=1000),
        cursor: str | None = Query(default=None, max_length=1024),
        action: str | None = Query(default=None, min_length=1, max_length=128),
        target_type: str | None = Query(default=None, min_length=1, max_length=128),
        target_id: str | None = Query(default=None, min_length=1, max_length=128),
        actor_principal_id: str | None = Query(
            default=None, min_length=1, max_length=128
        ),
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> Response:
        """Export one bounded NDJSON page; repeat with its cursor to continue."""
        try:
            events = services.governance.search_audit_events(
                action=action,
                target_type=target_type,
                target_id=target_id,
                actor_principal_id=actor_principal_id,
                created_after=created_after,
                created_before=created_before,
                limit=limit + 1,
                cursor=cursor,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        page = events[:limit]
        headers = {"Cache-Control": "no-store"}
        if len(events) > limit and page:
            headers["X-Next-Cursor"] = services.governance.audit_event_cursor(page[-1])
        return Response(
            content="".join(
                json.dumps(_dump(event), separators=(",", ":")) + "\n" for event in page
            ),
            media_type="application/x-ndjson",
            headers=headers,
        )

    app.get("/api/v0/audit-events/export")(export_audit_events)
