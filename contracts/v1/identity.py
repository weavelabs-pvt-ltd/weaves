from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from contracts.v1.base import (
    DisplayName,
    MutableOrgScopedContract,
    MutableProductContract,
    OpaqueId,
    ProductContract,
    StrEnum,
)


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class UserStatus(StrEnum):
    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PrincipalType(StrEnum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    SYSTEM = "system"
    EXTERNAL_APP = "external_app"


class Permission(StrEnum):
    ORGANIZATIONS_ADMIN = "organizations.admin"
    WORKSPACES_ADMIN = "workspaces.admin"
    USERS_MANAGE = "users.manage"
    ROLES_MANAGE = "roles.manage"
    MODELS_MANAGE = "models.manage"
    MODELS_VIEW = "models.view"
    AGENTS_MANAGE = "agents.manage"
    AGENTS_RUN = "agents.run"
    AGENTS_VIEW = "agents.view"
    WORKFLOWS_MANAGE = "workflows.manage"
    WORKFLOWS_RUN = "workflows.run"
    WORKFLOWS_VIEW = "workflows.view"
    TASKS_VIEW = "tasks.view"
    TASKS_CREATE = "tasks.create"
    TASKS_MANAGE = "tasks.manage"
    PLUGINS_MANAGE = "plugins.manage"
    PLUGINS_VIEW = "plugins.view"
    KNOWLEDGE_VIEW = "knowledge.view"
    ARTIFACTS_READ = "artifacts.read"
    ACTIONS_APPROVE = "actions.approve"
    APPROVAL_POLICIES_MANAGE = "approval_policies.manage"
    AUDIT_READ = "audit.read"


type SystemRoleKey = Literal["admin", "builder", "user"]


class Organization(MutableProductContract):
    id: OpaqueId
    name: DisplayName
    status: OrganizationStatus


class Workspace(MutableProductContract):
    id: OpaqueId
    org_id: OpaqueId
    name: DisplayName
    environment: DisplayName = "default"
    status: WorkspaceStatus


class User(MutableOrgScopedContract):
    id: OpaqueId
    email: str = Field(min_length=3, max_length=320)
    display_name: DisplayName
    status: UserStatus


class Principal(MutableOrgScopedContract):
    id: OpaqueId
    principal_type: PrincipalType
    status: PrincipalStatus
    user_id: OpaqueId | None = None

    @model_validator(mode="after")
    def user_reference_matches_type(self) -> "Principal":
        if self.principal_type is PrincipalType.USER and self.user_id is None:
            raise ValueError("user principals require user_id")
        if self.principal_type is not PrincipalType.USER and self.user_id is not None:
            raise ValueError("only user principals may include user_id")
        return self


class Role(ProductContract):
    id: OpaqueId
    org_id: OpaqueId
    name: DisplayName
    permissions: list[Permission] = Field(default_factory=list)
    system_role_key: SystemRoleKey | None = None

    @model_validator(mode="after")
    def permissions_are_unique(self) -> "Role":
        if len(self.permissions) != len(set(self.permissions)):
            raise ValueError("role permissions must be unique")
        return self


class RoleBinding(ProductContract):
    id: OpaqueId
    org_id: OpaqueId
    principal_id: OpaqueId
    role_id: OpaqueId
    workspace_id: OpaqueId | None = None
    created_at: AwareDatetime


__all__ = [
    "Organization",
    "OrganizationStatus",
    "Permission",
    "Principal",
    "PrincipalStatus",
    "PrincipalType",
    "Role",
    "RoleBinding",
    "SystemRoleKey",
    "User",
    "UserStatus",
    "Workspace",
    "WorkspaceStatus",
]
