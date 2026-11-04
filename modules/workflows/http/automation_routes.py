"""Workflow API route registrations migrated from the v0 API."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from application import IdempotencyConflict
from contracts.v1 import (
    WorkflowScheduleStatus,
)
from modules.workflows.automation_service import WorkflowAutomationService
from modules.workflows.http.schemas import (
    WorkflowScheduleCreateRequest,
    WorkflowSchedulePatchRequest,
    WorkflowTriggerCreateRequest,
    WorkflowTriggerPatchRequest,
)

WorkflowDump = Callable[[BaseModel], dict[str, Any]]


def _ensure_json_webhook_request(request: Request) -> None:
    """Reject non-JSON or oversized webhook requests before reading the body."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().casefold() != "application/json":
        raise HTTPException(
            status_code=415, detail="Content-Type must be application/json"
        )
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        if int(content_length) > 8_192:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc


async def _read_webhook_body(request: Request) -> bytes:
    """Read a webhook body while enforcing its configured byte limit."""
    raw_payload = bytearray()
    async for chunk in request.stream():
        raw_payload.extend(chunk)
        if len(raw_payload) > 8_192:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
    return bytes(raw_payload)


def _parse_webhook_payload(raw_payload: bytes) -> dict[str, JsonValue]:
    """Decode a JSON object and map malformed payloads to the public error."""
    try:
        decoded = json.loads(raw_payload.decode("utf-8"))
        return TypeAdapter(dict[str, JsonValue]).validate_python(decoded)
    except (ValueError, RecursionError, ValidationError) as exc:
        raise HTTPException(
            status_code=422, detail="Webhook body must be a JSON object"
        ) from exc


def _workflow_trigger_view(trigger: BaseModel) -> dict[str, Any]:
    """Serialize a trigger without exposing the stored secret digest."""
    result = trigger.model_dump(mode="json")
    result.pop("secret_digest", None)
    result["has_secret"] = True
    return result


def _register_list_workflow_triggers(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the list workflow triggers endpoint."""
    _dump = dump

    @app.get("/api/v0/workflow-triggers")
    def list_workflow_triggers() -> list[dict[str, Any]]:
        try:
            return [
                _workflow_trigger_view(item)
                for item in automation_service.list_workflow_triggers()
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def _register_create_workflow_trigger(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the create workflow trigger endpoint."""
    _dump = dump

    @app.post("/api/v0/workflows/{workflow_id}/triggers", status_code=201)
    def create_workflow_trigger(
        workflow_id: str, request: WorkflowTriggerCreateRequest
    ) -> dict[str, Any]:
        if not durable_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail=("Webhook workflow triggers require PostgreSQL-backed storage"),
            )
        try:
            trigger, token = automation_service.create_workflow_trigger(
                workflow_id,
                name=request.name,
                task_instructions=request.task_instructions,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "trigger": _workflow_trigger_view(trigger),
            "webhook_url": f"/api/v0/webhooks/{trigger.trigger_id}",
            "secret": token,
        }


def _register_list_workflow_schedules(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the list workflow schedules endpoint."""
    _dump = dump

    @app.get("/api/v0/workflow-schedules")
    def list_workflow_schedules() -> list[dict[str, Any]]:
        try:
            return [
                _dump(item) for item in automation_service.list_workflow_schedules()
            ]
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


def _register_create_workflow_schedule(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the create workflow schedule endpoint."""
    _dump = dump

    @app.post("/api/v0/workflows/{workflow_id}/schedules", status_code=201)
    def create_workflow_schedule(
        workflow_id: str, request: WorkflowScheduleCreateRequest
    ) -> dict[str, Any]:
        if not durable_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail="Workflow schedules require PostgreSQL-backed storage",
            )
        try:
            schedule = automation_service.create_workflow_schedule(
                workflow_id,
                name=request.name,
                task_instructions=request.task_instructions,
                interval_seconds=request.interval_seconds,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(schedule)


def _register_update_workflow_schedule(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the update workflow schedule endpoint."""
    _dump = dump

    @app.patch("/api/v0/workflow-schedules/{schedule_id}")
    def update_workflow_schedule(
        schedule_id: str, patch: WorkflowSchedulePatchRequest
    ) -> dict[str, Any]:
        try:
            if all(
                value is None
                for value in (
                    patch.status,
                    patch.name,
                    patch.task_instructions,
                    patch.interval_seconds,
                )
            ):
                raise ValueError("No schedule changes supplied")
            schedule = automation_service.update_workflow_schedule(
                schedule_id,
                WorkflowScheduleStatus(patch.status) if patch.status else None,
                name=patch.name,
                task_instructions=patch.task_instructions,
                interval_seconds=patch.interval_seconds,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Workflow schedule not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _dump(schedule)


def _register_update_workflow_trigger(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the update workflow trigger endpoint."""
    _dump = dump

    @app.patch("/api/v0/workflow-triggers/{trigger_id}")
    def update_workflow_trigger(
        trigger_id: str, patch: WorkflowTriggerPatchRequest
    ) -> dict[str, Any]:
        try:
            if (
                patch.status is None
                and patch.name is None
                and patch.task_instructions is None
            ):
                raise ValueError("No trigger changes supplied")
            trigger = automation_service.update_workflow_trigger(
                trigger_id,
                patch.status,
                name=patch.name,
                task_instructions=patch.task_instructions,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Workflow trigger not found"
            ) from exc
        return _workflow_trigger_view(trigger)


def _register_rotate_workflow_trigger_secret(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the rotate workflow trigger secret endpoint."""
    _dump = dump

    @app.put("/api/v0/workflow-triggers/{trigger_id}/secret")
    def rotate_workflow_trigger_secret(trigger_id: str) -> dict[str, Any]:
        try:
            trigger, token = automation_service.rotate_workflow_trigger_secret(
                trigger_id
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Workflow trigger not found"
            ) from exc
        return {"trigger": _workflow_trigger_view(trigger), "secret": token}


def _register_receive_workflow_webhook(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register the signed workflow webhook endpoint."""
    _dump = dump

    @app.post("/api/v0/webhooks/{trigger_id}", status_code=202)
    async def receive_workflow_webhook(
        trigger_id: str,
        request: Request,
        event_id: str = Header(alias="X-Event-ID", min_length=1, max_length=128),
        trigger_secret: str = Header(
            alias="X-Workflow-Trigger-Secret", min_length=1, max_length=512
        ),
    ) -> dict[str, Any]:
        if not durable_execution_enabled:
            raise HTTPException(
                status_code=503,
                detail=("Webhook workflow triggers require PostgreSQL-backed storage"),
            )
        _ensure_json_webhook_request(request)
        raw_payload = await _read_webhook_body(request)
        payload = _parse_webhook_payload(raw_payload)
        try:
            _, replayed = automation_service.enqueue_workflow_trigger_event(
                trigger_id, trigger_secret, event_id, payload
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="Workflow trigger not found"
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"accepted": True, "replayed": replayed}


def register_automation_routes(
    app: FastAPI,
    automation_service: WorkflowAutomationService,
    durable_execution_enabled: bool,
    dump: WorkflowDump,
) -> None:
    """Register automation routes."""
    _register_list_workflow_triggers(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_create_workflow_trigger(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_list_workflow_schedules(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_create_workflow_schedule(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_update_workflow_schedule(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_update_workflow_trigger(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_rotate_workflow_trigger_secret(
        app, automation_service, durable_execution_enabled, dump
    )
    _register_receive_workflow_webhook(
        app, automation_service, durable_execution_enabled, dump
    )
