"""Validated request schemas for the HTTP API."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
)

from contracts.v1 import (
    WorkflowConditionOperator,
    WorkflowStatus,
    WorkflowTriggerStatus,
)


class WorkflowRunCreateRequest(BaseModel):
    """Validated workflow run request input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task: str = Field(min_length=3, max_length=12000)
    thread_id: str | None = Field(default=None, max_length=128)


class WorkflowCreateRequest(BaseModel):
    """Validated workflow definition creation input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=2000)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=10)


class WorkflowPatchRequest(BaseModel):
    """Validated workflow metadata update fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    status: WorkflowStatus | None = None


class WorkflowStepsReplaceRequest(BaseModel):
    """Validated replacement workflow steps."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_ids: tuple[str, ...] = Field(min_length=1, max_length=10)
    step_conditions: tuple["WorkflowStepConditionRequest", ...] = Field(
        default=(), max_length=10
    )
    parallel_groups: tuple["WorkflowParallelGroupRequest", ...] = Field(
        default=(), max_length=5
    )
    step_dependencies: tuple["WorkflowStepDependencyRequest", ...] = Field(
        default=(), max_length=9
    )


class WorkflowStepConditionRequest(BaseModel):
    """Validated conditional workflow branch input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_agent_id: str = Field(min_length=1, max_length=128)
    target_agent_id: str = Field(min_length=1, max_length=128)
    json_pointer: str = Field(max_length=512)
    operator: WorkflowConditionOperator
    expected_value: JsonValue | None = None


class WorkflowParallelGroupRequest(BaseModel):
    """Validated parallel workflow group input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_ids: tuple[str, ...] = Field(min_length=2, max_length=5)


class WorkflowStepDependencyRequest(BaseModel):
    """Validated workflow dependency declaration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_agent_id: str = Field(min_length=1, max_length=128)
    depends_on_agent_ids: tuple[str, ...] = Field(min_length=1, max_length=9)
    join_policy: Literal["all", "any"] = "all"


class WorkflowTriggerCreateRequest(BaseModel):
    """Validated workflow trigger creation input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    task_instructions: str = Field(min_length=1, max_length=2000)


class WorkflowTriggerPatchRequest(BaseModel):
    """Validated workflow trigger update fields."""

    model_config = ConfigDict(extra="forbid")

    status: WorkflowTriggerStatus


class WorkflowScheduleCreateRequest(BaseModel):
    """Validated workflow schedule creation input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    task_instructions: str = Field(min_length=1, max_length=2000)
    interval_seconds: int = Field(ge=60, le=31_536_000)


class WorkflowSchedulePatchRequest(BaseModel):
    """Validated workflow schedule update fields."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"]
