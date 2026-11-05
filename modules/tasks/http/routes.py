"""HTTP endpoints for durable task conversations."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from contracts.v1 import TaskStatus
from modules.tasks.http.cursors import decode_cursor, encode_cursor
from modules.tasks.service import TaskService


class TaskCreateRequest(BaseModel):
    """Fields required to create a task and submit its first turn."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    agent_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    initial_message: str = Field(min_length=1, max_length=12_000)


class TaskMessageRequest(BaseModel):
    """Content of a new user turn in an open task."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=12_000)


def register_task_routes(
    app: FastAPI,
    service: TaskService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register list, create, transcript, and lifecycle task endpoints."""
    _register_task_collection_routes(app, service, dump)

    @app.get("/api/v0/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        return dump(_call(lambda: service.get_task(task_id)))

    @app.get("/api/v0/tasks/{task_id}/messages")
    def list_messages(
        task_id: str,
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        cursor_time, cursor_id = _decode_http_cursor(cursor)
        page = _call(
            lambda: service.list_messages(
                task_id,
                limit=limit + 1,
                cursor_created_at=cursor_time,
                cursor_message_id=cursor_id,
            )
        )
        items = page[-limit:]
        next_cursor = (
            encode_cursor(items[0].created_at, items[0].message_id)
            if len(page) > limit
            else None
        )
        return {"items": [dump(item) for item in items], "next_cursor": next_cursor}

    @app.post("/api/v0/tasks/{task_id}/messages", status_code=202)
    def add_message(
        task_id: str,
        request: TaskMessageRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        turn = _call(
            lambda: service.add_message(
                task_id, request.content, idempotency_key=idempotency_key
            )
        )
        return dump(turn)

    @app.post("/api/v0/tasks/{task_id}/complete")
    def complete_task(task_id: str) -> dict[str, Any]:
        return dump(_call(lambda: service.complete_task(task_id)))

    @app.post("/api/v0/tasks/{task_id}/turns/{turn_id}/retry", status_code=202)
    def retry_turn(task_id: str, turn_id: str) -> dict[str, Any]:
        return dump(_call(lambda: service.retry_turn(task_id, turn_id)))

    @app.post("/api/v0/tasks/{task_id}/archive")
    def archive_task(task_id: str) -> dict[str, Any]:
        return dump(_call(lambda: service.archive_task(task_id)))

    @app.post("/api/v0/tasks/{task_id}/reopen")
    def reopen_task(task_id: str) -> dict[str, Any]:
        return dump(_call(lambda: service.reopen_task(task_id)))


def _register_task_collection_routes(
    app: FastAPI,
    service: TaskService,
    dump: Callable[[BaseModel], dict[str, Any]],
) -> None:
    """Register task creation and paginated workspace listing endpoints."""

    @app.get("/api/v0/tasks")
    def list_tasks(
        status: TaskStatus | None = None,
        agent_id: str | None = None,
        created_by_principal_id: str | None = None,
        include_execution: bool = False,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        cursor_time, cursor_id = _decode_http_cursor(cursor)
        page = _call(
            lambda: service.list_tasks(
                status=status,
                agent_id=agent_id,
                created_by_principal_id=created_by_principal_id,
                limit=limit + 1,
                cursor_created_at=cursor_time,
                cursor_task_id=cursor_id,
            )
        )
        items = page[:limit]
        next_cursor = (
            encode_cursor(items[-1].created_at, items[-1].task_id)
            if len(page) > limit
            else None
        )
        execution = (
            _call(lambda: service.execution_summaries(tuple(items)))
            if include_execution
            else {}
        )
        return {
            "items": [
                {
                    **dump(item),
                    **(
                        {"execution": execution.get(item.task_id)}
                        if include_execution
                        else {}
                    ),
                }
                for item in items
            ],
            "next_cursor": next_cursor,
        }

    @app.post("/api/v0/tasks", status_code=202)
    def create_task(
        request: TaskCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        task = _call(
            lambda: service.create_task(
                agent_id=request.agent_id,
                title=request.title,
                description=request.description,
                initial_message=request.initial_message,
                idempotency_key=idempotency_key,
            )
        )
        return dump(task)


def _call(operation: Callable[[], Any]) -> Any:
    """Map domain authorization, missing records, and validation errors."""
    try:
        return operation()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _decode_http_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    """Convert an optional query cursor into a validated keyset position."""
    if cursor is None:
        return None, None
    try:
        return decode_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
