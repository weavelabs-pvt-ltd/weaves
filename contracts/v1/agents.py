from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
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
    thaw_json_value,
    validate_json_schema,
)
from contracts.v1.memory import MemoryPolicy

InstructionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)
]
PersonaText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
PositiveVersion = Annotated[int, Field(ge=1)]


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentBudget(FrozenProductContract):
    max_runtime_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
    max_tool_calls: Annotated[int, Field(ge=0, le=100)] = 10
    max_output_tokens: Annotated[int, Field(ge=1, le=32768)] = 4096
    max_run_cost_usd: (
        Annotated[Decimal, Field(gt=Decimal("0"), le=Decimal("100000"))] | None
    ) = None


class AgentDefinition(MutableWorkspaceScopedContract):
    id: OpaqueId
    name: DisplayName
    description: str = Field(default="", max_length=1000)
    current_version_id: OpaqueId | None = None
    status: AgentStatus

    @model_validator(mode="after")
    def active_agent_has_published_version(
        self: "AgentDefinition",
    ) -> "AgentDefinition":
        if self.status is AgentStatus.ACTIVE and self.current_version_id is None:
            raise ValueError("active agents require a current published version")
        return self


class AgentVersion(ImmutableWorkspaceVersionContract):
    agent_version_id: OpaqueId
    agent_id: OpaqueId
    version: PositiveVersion
    persona: PersonaText
    instructions: InstructionText
    model_profile_id: OpaqueId
    allowed_capability_ids: tuple[OpaqueId, ...] = Field(
        default_factory=tuple, max_length=100
    )
    allowed_workflow_ids: tuple[OpaqueId, ...] = Field(
        default_factory=tuple, max_length=100
    )
    approval_policy_id: OpaqueId | None = None
    budget_policy_id: OpaqueId | None = None
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    output_schema: dict[str, JsonValue] | None = None

    @field_validator("output_schema")
    @classmethod
    def output_schema_is_frozen_json(
        cls: type["AgentVersion"], value: dict[str, JsonValue] | None
    ) -> Any:
        if value is None:
            return value
        return validate_json_schema(value, "output_schema")

    @field_serializer("output_schema")
    def serialize_output_schema(self: "AgentVersion", value: Any) -> Any:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def allowlists_are_unique(self: "AgentVersion") -> "AgentVersion":
        if len(self.allowed_capability_ids) != len(set(self.allowed_capability_ids)):
            raise ValueError("allowed_capability_ids must be unique")
        if len(self.allowed_workflow_ids) != len(set(self.allowed_workflow_ids)):
            raise ValueError("allowed_workflow_ids must be unique")
        return self


class AgentBundle(ProductContract):
    """Resolved agent definition/version pair with scope and pointer checks."""

    definition: AgentDefinition
    version: AgentVersion

    @model_validator(mode="after")
    def version_must_match_definition(self: "AgentBundle") -> "AgentBundle":
        if self.version.agent_id != self.definition.id:
            raise ValueError("agent version must reference the supplied definition")
        if self.version.org_id != self.definition.org_id:
            raise ValueError("agent definition and version must share org_id")
        if self.version.workspace_id != self.definition.workspace_id:
            raise ValueError("agent definition and version must share workspace_id")
        if self.definition.current_version_id != self.version.agent_version_id:
            raise ValueError("agent definition current version pointer must match")
        return self


__all__ = [
    "AgentBudget",
    "AgentBundle",
    "AgentDefinition",
    "AgentStatus",
    "AgentVersion",
]
