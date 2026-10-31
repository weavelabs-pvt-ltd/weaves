from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints


class StrEnum(str, Enum):
    """String enum available on the project's full Python 3.9+ range."""

    def __str__(self) -> str:
        return str(self.value)


OpaqueId: TypeAlias = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
DisplayName: TypeAlias = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)
]


class ProductContract(BaseModel):
    """Base for published product payloads; rejects undeclared fields."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1


class MutableProductContract(ProductContract):
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ImmutableProductContract(ProductContract):
    created_at: AwareDatetime


class ImmutableVersionContract(ImmutableProductContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ImmutableWorkspaceVersionContract(ImmutableVersionContract):
    org_id: OpaqueId
    workspace_id: OpaqueId


class FrozenProductContract(ProductContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class OrgScopedContract(ProductContract):
    org_id: OpaqueId


class MutableOrgScopedContract(MutableProductContract):
    org_id: OpaqueId


class WorkspaceScopedContract(OrgScopedContract):
    workspace_id: OpaqueId


class MutableWorkspaceScopedContract(MutableOrgScopedContract):
    workspace_id: OpaqueId
