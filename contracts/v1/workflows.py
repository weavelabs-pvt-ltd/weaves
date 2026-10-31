from typing import Annotated, Any

from pydantic import (
    Field,
    JsonValue,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from contracts.v1.base import (
    DisplayName,
    FrozenProductContract,
    ImmutableWorkspaceVersionContract,
    MutableWorkspaceScopedContract,
    OpaqueId,
    ProductContract,
    StrEnum,
)
from contracts.v1.json_data import (
    freeze_json_value,
    reject_credential_fields,
    thaw_json_value,
    validate_json_schema,
)
from contracts.v1.plugins import Identifier, JsonObject

PositiveVersion = Annotated[int, Field(ge=1)]


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkflowConditionOperator(StrEnum):
    EXISTS = "exists"
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"


class WorkflowStepCondition(FrozenProductContract):
    """Allow a workflow step only when a prior structured result matches."""

    source_agent_id: OpaqueId
    target_agent_id: OpaqueId
    json_pointer: str = Field(max_length=512)
    operator: WorkflowConditionOperator
    expected_value: JsonValue

    @field_validator("json_pointer")
    @classmethod
    def pointer_is_valid(cls: type["WorkflowStepCondition"], value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("json_pointer must be empty or start with '/'")
        if any(
            part.replace("~0", "").replace("~1", "").find("~") >= 0
            for part in value.split("/")[1:]
        ):
            raise ValueError("json_pointer contains an invalid escape")
        return value

    @field_validator("expected_value")
    @classmethod
    def expected_value_is_frozen(
        cls: type["WorkflowStepCondition"], value: JsonValue
    ) -> Any:
        if value is None:
            return None
        reject_credential_fields(value, "expected_value")
        return freeze_json_value(value, "expected_value")

    @field_serializer("expected_value")
    def serialize_expected_value(self: "WorkflowStepCondition", value: Any) -> Any:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def operands_match_operator(self) -> "WorkflowStepCondition":
        if self.source_agent_id == self.target_agent_id:
            raise ValueError(
                "a workflow condition must reference a different source agent"
            )
        if (
            self.operator is WorkflowConditionOperator.EXISTS
            and self.expected_value is not None
        ):
            raise ValueError("exists conditions must not set expected_value")
        return self


class WorkflowParallelGroup(FrozenProductContract):
    """A bounded set of adjacent workflow agents that can execute together."""

    agent_ids: tuple[OpaqueId, ...] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def agent_ids_are_unique(self) -> "WorkflowParallelGroup":
        if len(self.agent_ids) != len(set(self.agent_ids)):
            raise ValueError("parallel group agent_ids must be unique")
        return self


class WorkflowJoinPolicy(StrEnum):
    ALL = "all"
    ANY = "any"


class WorkflowStepDependency(FrozenProductContract):
    """Predecessor steps that must finish before a workflow step is eligible."""

    target_agent_id: OpaqueId
    depends_on_agent_ids: tuple[OpaqueId, ...] = Field(min_length=1, max_length=9)
    join_policy: WorkflowJoinPolicy = WorkflowJoinPolicy.ALL

    @model_validator(mode="after")
    def dependency_ids_are_unique(self) -> "WorkflowStepDependency":
        if len(self.depends_on_agent_ids) != len(set(self.depends_on_agent_ids)):
            raise ValueError("depends_on_agent_ids must be unique")
        if self.target_agent_id in self.depends_on_agent_ids:
            raise ValueError("a workflow step cannot depend on itself")
        return self


class WorkflowDefinition(MutableWorkspaceScopedContract):
    workflow_id: OpaqueId
    name: DisplayName
    description: str = Field(default="", max_length=2000)
    current_version_id: OpaqueId | None = None
    status: WorkflowStatus

    @model_validator(mode="after")
    def active_workflow_has_published_version(
        self: "WorkflowDefinition",
    ) -> "WorkflowDefinition":
        if self.status is WorkflowStatus.ACTIVE and self.current_version_id is None:
            raise ValueError("active workflows require a current published version")
        return self


class WorkflowVersion(ImmutableWorkspaceVersionContract):
    workflow_version_id: OpaqueId
    workflow_id: OpaqueId
    version: PositiveVersion
    handler_key: Identifier
    agent_version_ids: tuple[OpaqueId, ...] = Field(min_length=1, max_length=50)
    required_capability_ids: tuple[Identifier, ...] = Field(
        default_factory=tuple, max_length=200
    )
    step_conditions: tuple[WorkflowStepCondition, ...] = Field(
        default_factory=tuple, max_length=10
    )
    parallel_groups: tuple[WorkflowParallelGroup, ...] = Field(
        default_factory=tuple, max_length=5
    )
    step_dependencies: tuple[WorkflowStepDependency, ...] = Field(
        default_factory=tuple, max_length=9
    )
    input_schema: JsonObject
    output_schema: JsonObject
    configuration: JsonObject = Field(default_factory=dict)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schemas_are_valid_and_frozen(
        cls: type["WorkflowVersion"], value: JsonObject, info: ValidationInfo
    ) -> Any:
        return validate_json_schema(value, info.field_name or "schema")

    @field_validator("configuration")
    @classmethod
    def configuration_is_json_and_frozen(
        cls: type["WorkflowVersion"], value: JsonObject
    ) -> Any:
        reject_credential_fields(value, "configuration")
        return freeze_json_value(value, "configuration")

    @field_serializer("input_schema", "output_schema", "configuration")
    def serialize_json_values(self: "WorkflowVersion", value: Any) -> Any:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def referenced_ids_are_unique(self: "WorkflowVersion") -> "WorkflowVersion":
        if len(self.agent_version_ids) != len(set(self.agent_version_ids)):
            raise ValueError("agent_version_ids must be unique")
        if len(self.required_capability_ids) != len(set(self.required_capability_ids)):
            raise ValueError("required_capability_ids must be unique")
        target_ids = tuple(item.target_agent_id for item in self.step_conditions)
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("workflow steps can have at most one condition each")
        dependency_targets = tuple(
            item.target_agent_id for item in self.step_dependencies
        )
        if len(dependency_targets) != len(set(dependency_targets)):
            raise ValueError("workflow steps can have only one dependency policy")
        parallel_agent_ids = tuple(
            agent_id for group in self.parallel_groups for agent_id in group.agent_ids
        )
        if len(parallel_agent_ids) != len(set(parallel_agent_ids)):
            raise ValueError("parallel groups cannot share workflow steps")
        return self


class WorkflowBundle(ProductContract):
    definition: WorkflowDefinition
    version: WorkflowVersion

    @model_validator(mode="after")
    def version_matches_definition(self: "WorkflowBundle") -> "WorkflowBundle":
        if self.version.workflow_id != self.definition.workflow_id:
            raise ValueError("workflow version must reference the supplied definition")
        if self.version.org_id != self.definition.org_id:
            raise ValueError("workflow definition and version must share org_id")
        if self.version.workspace_id != self.definition.workspace_id:
            raise ValueError("workflow definition and version must share workspace_id")
        if self.definition.current_version_id != self.version.workflow_version_id:
            raise ValueError("workflow definition current version pointer must match")
        return self


__all__ = [
    "WorkflowBundle",
    "WorkflowConditionOperator",
    "WorkflowDefinition",
    "WorkflowJoinPolicy",
    "WorkflowParallelGroup",
    "WorkflowStatus",
    "WorkflowStepCondition",
    "WorkflowStepDependency",
    "WorkflowVersion",
]
