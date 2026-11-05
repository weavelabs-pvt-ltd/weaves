"""Versioned workflow canvas plans and durable execution progress."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from contracts.v1.base import MutableWorkspaceScopedContract


class CanvasStep(BaseModel):
    """One independently addressable step; agents may appear more than once."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=128)
    type: Literal["agent", "par", "cond", "appr"]
    a: list[str] = Field(default_factory=list, max_length=3)
    t: str = Field(min_length=1, max_length=2000)
    fail: Literal["stop", "retry", "skip"] = "stop"


class Calendar(BaseModel):
    """Weekly calendar in the reference's explicit IST time zone."""

    model_config = ConfigDict(extra="forbid")
    freq: Literal["hourly", "daily", "weekly"] = "daily"
    days: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7
    )
    time: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class CanvasPlan(BaseModel):
    """The published plan consumed by the canvas and execution worker."""

    model_config = ConfigDict(extra="forbid")
    steps: list[CanvasStep] = Field(min_length=1, max_length=50)
    manual: bool = True
    hook: bool = False
    event: str = Field(default="custom.event", min_length=1, max_length=160)
    sched: bool = False
    calendar: Calendar = Field(default_factory=Calendar)


class CanvasSettings(MutableWorkspaceScopedContract):
    """Mutable start controls, separate from immutable step versions."""

    workflow_id: str
    principal_id: str
    manual: bool = True
    hook: bool = False
    event: str = "custom.event"
    sched: bool = False
    calendar: dict[str, JsonValue] = Field(default_factory=dict)
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None


class CanvasRun(MutableWorkspaceScopedContract):
    """Durable workflow execution; each child task uses the existing task queue."""

    run_id: str
    job_id: str
    workflow_id: str
    workflow_version_id: str
    requested_by_principal_id: str
    instructions: str
    context: list[str] = Field(default_factory=list)
    trigger_type: str = "manual"
    idempotency_key: str | None = None
    status: str = "queued"
    plan: dict[str, JsonValue]
    states: list[dict[str, JsonValue]]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: str = ""
    error: str | None = None
    retry_of: str | None = None
    agent_versions: dict[str, str] = Field(default_factory=dict)
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list)
