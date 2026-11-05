"""Validated request schemas for the HTTP API."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from contracts.v1 import Permission, WorkspaceStatus


class ServiceAccountCreateRequest(BaseModel):
    """Validated service account creation input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=160)
    permissions: list[Permission] = Field(min_length=1, max_length=20)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class OrganizationOnboardingRequest(BaseModel):
    """Validated organization owner and workspace input."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=160)
    owner_email: str = Field(min_length=3, max_length=320)
    owner_display_name: str = Field(min_length=1, max_length=160)


class UserLoginRequest(BaseModel):
    """Validated user password sign-in input."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=12, max_length=1024)
    organization_id: str | None = Field(default=None, min_length=1, max_length=128)


class EmailLoginCodeRequest(BaseModel):
    """Request an email code for an existing, provisioned organization member."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    organization_id: str | None = Field(default=None, min_length=1, max_length=128)


class EmailLoginVerifyRequest(BaseModel):
    """Verify one email code and create a browser session."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r"^\d{6}$", min_length=6, max_length=6)


class UserPasswordRequest(BaseModel):
    """Validated password change input."""

    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=12, max_length=1024)
    current_password: SecretStr | None = Field(default=None, max_length=1024)


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
