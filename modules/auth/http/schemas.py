"""Validated request schemas for the HTTP API."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.v1 import Permission, WorkspaceStatus


class ServiceAccountCreateRequest(BaseModel):
    """Validated service account creation input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=160)
    permissions: list[Permission] = Field(min_length=1, max_length=20)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class WorkspaceCreateRequest(BaseModel):
    """Validated workspace creation input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    environment: str = Field(default="default", min_length=1, max_length=160)


class WorkspacePatchRequest(BaseModel):
    """Validated workspace update fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=160)
    environment: str | None = Field(default=None, min_length=1, max_length=160)
    status: WorkspaceStatus | None = None


class WorkspaceUserCreateRequest(BaseModel):
    """Validated user creation and role assignment input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=160)
    permissions: list[Permission] | None = Field(default=None, max_length=20)
    role_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def has_one_role_source(self) -> "WorkspaceUserCreateRequest":
        """Require one role ID or an explicit permission list."""
        if (self.role_id is None) == (not self.permissions):
            raise ValueError("provide exactly one of role_id or permissions")
        return self


class RoleCreateRequest(BaseModel):
    """Validated role name and permission input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=160)
    permissions: list[Permission] = Field(min_length=1, max_length=20)


class RoleBindingCreateRequest(BaseModel):
    """Validated principal-to-role assignment input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    principal_id: str = Field(min_length=1, max_length=128)
    role_id: str = Field(min_length=1, max_length=128)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
