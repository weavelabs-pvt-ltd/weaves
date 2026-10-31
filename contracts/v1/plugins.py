import json
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
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
    MutableWorkspaceScopedContract,
    OpaqueId,
    ProductContract,
    StrEnum,
)
from contracts.v1.json_data import (
    reject_credential_fields,
    thaw_json_value,
    validate_json_schema,
)

Identifier = Annotated[
    str,
    Field(min_length=2, max_length=128, pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"),
]
JsonObject = dict[str, JsonValue]


class CapabilityKind(StrEnum):
    CONTEXT_READ = "context.read"
    ACTION_WRITE = "action.write"
    DELIVERY_SEND = "delivery.send"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PluginDescriptorStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class PluginInstallationStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


class CapabilitySpec(FrozenProductContract):
    capability_id: Identifier
    name: DisplayName
    description: str = Field(default="", max_length=1000)
    kind: CapabilityKind
    risk_level: RiskLevel | None = None
    input_schema: JsonObject
    output_schema: JsonObject
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    approval_supported: bool = False

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schemas_are_valid_and_frozen(
        cls: type["CapabilitySpec"], value: JsonObject, info: ValidationInfo
    ) -> Any:
        return validate_json_schema(value, info.field_name or "schema")

    @field_serializer("input_schema", "output_schema")
    def serialize_schemas(self: "CapabilitySpec", value: Any) -> Any:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def risky_capability_has_risk_level(self: "CapabilitySpec") -> "CapabilitySpec":
        if self.kind in {CapabilityKind.ACTION_WRITE, CapabilityKind.DELIVERY_SEND}:
            if self.risk_level is None:
                raise ValueError("write and delivery capabilities require risk_level")
        return self


class PluginDescriptor(FrozenProductContract):
    plugin_id: OpaqueId
    plugin_type: Identifier
    display_name: DisplayName
    description: str = Field(default="", max_length=2000)
    capability_manifest: tuple[CapabilitySpec, ...] = Field(
        min_length=1, max_length=200
    )
    auth_schema: JsonObject | None = None
    configuration_schema: JsonObject | None = None
    status: PluginDescriptorStatus

    @field_validator("auth_schema", "configuration_schema")
    @classmethod
    def schemas_are_valid_and_frozen(
        cls: type["PluginDescriptor"],
        value: JsonObject | None,
        info: ValidationInfo,
    ) -> Any:
        if value is None:
            return None
        return validate_json_schema(value, info.field_name or "schema")

    @field_serializer("auth_schema", "configuration_schema")
    def serialize_schemas(self: "PluginDescriptor", value: Any) -> Any:
        return thaw_json_value(value)

    @model_validator(mode="after")
    def manifest_capability_ids_are_unique(
        self: "PluginDescriptor",
    ) -> "PluginDescriptor":
        capability_ids = [item.capability_id for item in self.capability_manifest]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capability_manifest IDs must be unique")
        return self


class PluginInstallation(MutableWorkspaceScopedContract):
    plugin_installation_id: OpaqueId
    plugin_id: OpaqueId
    display_name: DisplayName
    auth_ref: OpaqueId | None = None
    configuration: JsonObject = Field(default_factory=dict)
    enabled_capability_ids: tuple[Identifier, ...] = Field(
        default_factory=tuple, max_length=200
    )
    status: PluginInstallationStatus
    last_healthcheck_at: AwareDatetime | None = None

    @field_validator("configuration")
    @classmethod
    def config_is_non_secret_json(
        cls: type["PluginInstallation"], value: JsonObject
    ) -> JsonObject:
        # Pydantic has validated JSON value types; this also rejects NaN/Infinity.
        json.dumps(value, allow_nan=False)
        reject_credential_fields(value, "configuration")
        return value

    @model_validator(mode="after")
    def enabled_capability_ids_are_unique(
        self: "PluginInstallation",
    ) -> "PluginInstallation":
        ids = self.enabled_capability_ids
        if len(ids) != len(set(ids)):
            raise ValueError("enabled_capability_ids must be unique")
        return self


class PluginInstallationBinding(ProductContract):
    descriptor: PluginDescriptor
    installation: PluginInstallation

    @model_validator(mode="after")
    def installation_matches_descriptor(
        self: "PluginInstallationBinding",
    ) -> "PluginInstallationBinding":
        if self.installation.plugin_id != self.descriptor.plugin_id:
            raise ValueError("installation must reference the supplied plugin")
        declared = {item.capability_id for item in self.descriptor.capability_manifest}
        if not set(self.installation.enabled_capability_ids).issubset(declared):
            raise ValueError("installation can enable only declared capabilities")
        return self


__all__ = [
    "CapabilityKind",
    "CapabilitySpec",
    "PluginDescriptor",
    "PluginDescriptorStatus",
    "PluginInstallation",
    "PluginInstallationBinding",
    "PluginInstallationStatus",
    "RiskLevel",
]
