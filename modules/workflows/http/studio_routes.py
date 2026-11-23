"""HTTP surface for complete canvas publication and durable run inspection."""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from application.http.pagination import RecentPageParams
from application.pagination import encode_time_cursor
from contracts.v1 import Permission
from contracts.v1.timezones import ScheduleTimezone
from contracts.v1.workflow_studio import Calendar, CanvasPlan, CanvasRun, CanvasSettings
from modules.workflows.schedule_time import effective_timezone
from modules.workflows.studio_service import WorkflowStudioService
from shared.errors import StaleRevision


class PublishCanvas(BaseModel):
    """Validated publication of a complete canvas."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    workflow_id: str | None = None
    name: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=2000)
    plan: CanvasPlan
    timezone: ScheduleTimezone | None = None


class LaunchCanvas(BaseModel):
    """One idempotent run pinned to the published version."""

    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(default="Run with default instructions", max_length=12000)
    context: list[str] = Field(default_factory=list, max_length=20)
    retry_of: str | None = None


class StartControls(BaseModel):
    """Mutable workflow start controls."""

    model_config = ConfigDict(extra="forbid")
    manual: bool | None = None
    hook: bool | None = None
    sched: bool | None = None
    event: str | None = Field(default=None, min_length=1, max_length=160)
    calendar: Calendar | None = None
    timezone: ScheduleTimezone | None = None
    expected_updated_at: AwareDatetime | None = None


def _settings_view(item: CanvasSettings) -> dict[str, JsonValue]:
    """Settings read model: legacy records report their effective zone."""
    view = item.model_dump(mode="json")
    view["timezone"] = effective_timezone(item.timezone)
    return view


def register_studio_routes(app: FastAPI, studio: WorkflowStudioService) -> None:
    """Keep transport error mapping separate from workflow use cases."""
    _register_start_routes(app, studio)

    @app.post("/api/v0/workflow-studio/publish")
    def publish(body: PublishCanvas) -> dict[str, dict[str, JsonValue]]:
        return call(
            studio.publish,
            body.name,
            body.description,
            body.plan,
            body.workflow_id,
            body.timezone,
        )

    @app.get("/api/v0/workflows/{workflow_id}/runs")
    def runs(workflow_id: str, page: RecentPageParams) -> dict[str, Any]:
        bound = page.limit or 100
        rows = call(
            studio.list_runs,
            workflow_id,
            limit=bound + 1,
            cursor_created_at=page.cursor_created_at,
            cursor_id=page.cursor_id,
        )
        page_rows = rows[:bound]
        return {
            "items": page_rows,
            "next_cursor": encode_time_cursor(
                page_rows[-1].created_at, page_rows[-1].run_id
            )
            if len(rows) > bound
            else None,
        }

    @app.post("/api/v0/workflows/{workflow_id}/runs", status_code=202)
    def launch(
        workflow_id: str,
        body: LaunchCanvas,
        idempotency_key: str | None = Header(default=None, max_length=128),
    ) -> CanvasRun:
        return call(
            studio.start,
            workflow_id,
            body.instructions,
            body.context,
            idempotency_key,
            retry=body.retry_of,
        )

    @app.get("/api/v0/workflows/{workflow_id}/runs/{run_id}")
    def run(workflow_id: str, run_id: str) -> CanvasRun:
        return call(studio.get_run, workflow_id, run_id)

    @app.post("/api/v0/workflows/{workflow_id}/runs/{run_id}/{action}")
    def decision(
        workflow_id: str, run_id: str, action: Literal["stop", "approve", "reject"]
    ) -> CanvasRun:
        return call(studio.decide, workflow_id, run_id, action)


def _register_start_routes(app: FastAPI, studio: WorkflowStudioService) -> None:
    """Register mutable trigger settings without touching version snapshots."""

    @app.get("/api/v0/workflows/{workflow_id}/studio/settings")
    def settings(workflow_id: str) -> dict[str, JsonValue]:
        item = call(studio.get_settings, workflow_id)
        result = _settings_view(item)
        result["next_runs"] = [
            d.isoformat()
            for d in studio.next_dates(item.calendar, timezone=item.timezone)
        ]
        return result

    @app.patch("/api/v0/workflows/{workflow_id}/studio/settings")
    def save_settings(workflow_id: str, body: StartControls) -> dict[str, JsonValue]:
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise HTTPException(422, "No changes supplied")
        return _settings_view(call(studio.save_settings, workflow_id, changes))

    @app.post("/api/v0/workflows/{workflow_id}/studio/test-webhook")
    def test_webhook(workflow_id: str) -> dict[str, bool]:
        call(studio.authorize, Permission.WORKFLOWS_MANAGE)
        call(studio.definitions.get_workflow, workflow_id)
        return {"received": True, "run_started": False}


def call[**P, T](fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Translate domain failures at the HTTP boundary."""
    try:
        return fn(*args, **kwargs)
    except StaleRevision as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Workflow or run not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
