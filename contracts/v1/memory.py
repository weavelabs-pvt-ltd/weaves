"""Explicit, tenant-scoped memory records and agent retrieval policy."""

from pydantic import AwareDatetime, Field, model_validator

from contracts.v1.base import (
    DisplayName,
    FrozenProductContract,
    MutableOrgScopedContract,
    OpaqueId,
    StrEnum,
)


class MemoryRetentionClass(StrEnum):
    EPHEMERAL = "ephemeral"
    STANDARD = "standard"
    REGULATED = "regulated"
    LOCKED = "locked"


class MemoryScope(StrEnum):
    NONE = "none"
    THREAD = "thread"
    WORKFLOW = "workflow"
    WORKSPACE = "workspace"
    ORGANIZATION = "organization"


class MemoryPolicy(FrozenProductContract):
    """Scopes an agent may retrieve during a run; defaults to workspace memory."""

    allowed_scopes: tuple[MemoryScope, ...] = (MemoryScope.WORKSPACE,)
    max_items: int = Field(default=8, ge=0, le=50)

    @model_validator(mode="after")
    def scopes_are_consistent(self) -> "MemoryPolicy":
        if len(self.allowed_scopes) != len(set(self.allowed_scopes)):
            raise ValueError("memory scopes must be unique")
        if MemoryScope.NONE in self.allowed_scopes and len(self.allowed_scopes) != 1:
            raise ValueError("none cannot be combined with memory scopes")
        if MemoryScope.NONE in self.allowed_scopes and self.max_items != 0:
            raise ValueError("max_items must be zero when memory is disabled")
        return self


class MemoryItem(MutableOrgScopedContract):
    memory_id: OpaqueId
    scope: MemoryScope
    workspace_id: OpaqueId | None = None
    scope_ref: OpaqueId | None = None
    title: DisplayName
    text: str = Field(min_length=1, max_length=12_000)
    source_ref: str | None = Field(default=None, max_length=256)
    retention_class: MemoryRetentionClass = MemoryRetentionClass.STANDARD
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def scope_fields_match(self) -> "MemoryItem":
        if self.scope is MemoryScope.WORKSPACE and not self.workspace_id:
            raise ValueError("workspace memory requires workspace_id")
        if self.scope in {MemoryScope.THREAD, MemoryScope.WORKFLOW}:
            if not self.workspace_id or not self.scope_ref:
                raise ValueError(
                    "thread and workflow memory require workspace_id and scope_ref"
                )
        if self.scope is MemoryScope.ORGANIZATION and (
            self.workspace_id or self.scope_ref
        ):
            raise ValueError(
                "organization memory cannot specify workspace_id or scope_ref"
            )
        if self.scope is MemoryScope.WORKSPACE and self.scope_ref:
            raise ValueError("workspace memory cannot specify scope_ref")
        if self.scope is MemoryScope.NONE:
            raise ValueError("memory items cannot use the none scope")
        if (
            self.retention_class is MemoryRetentionClass.EPHEMERAL
            and not self.expires_at
        ):
            raise ValueError("ephemeral memory requires expires_at")
        if self.expires_at and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


__all__ = ["MemoryItem", "MemoryPolicy", "MemoryRetentionClass", "MemoryScope"]
