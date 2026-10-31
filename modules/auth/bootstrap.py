"""Seed the local developer identity and its platform role."""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar

from application.ports.repository import Repository
from contracts.v1 import (
    Organization,
    OrganizationStatus,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    User,
    UserStatus,
    Workspace,
    WorkspaceStatus,
)
from contracts.v1.base import ProductContract

TProduct = TypeVar("TProduct", bound=ProductContract)


class IdentityBootstrapService:
    """Create the deterministic developer account used by local operation."""

    def __init__(
        self,
        *,
        users: Repository[User],
        principals: Repository[Principal],
        roles: Repository[Role],
        organizations: Repository[Organization],
        workspaces: Repository[Workspace],
        role_bindings: Repository[RoleBinding],
    ) -> None:
        """Bind identity repositories without depending on the platform runtime."""
        self.users = users
        self.principals = principals
        self.roles = roles
        self.organizations = organizations
        self.workspaces = workspaces
        self.role_bindings = role_bindings

    @staticmethod
    def _seed(
        repository: Repository[TProduct], id_field: str, record: TProduct
    ) -> TProduct:
        """Create a deterministic starter record only when it is absent."""
        record_id = str(getattr(record, id_field))
        try:
            return repository.get(record_id)
        except KeyError:
            try:
                return repository.create(record)
            except ValueError:
                return repository.get(record_id)

    def seed_local_developer(self, now: datetime) -> None:
        """Seed the developer user, principal, and complete permission role."""
        self._seed(
            self.users,
            "id",
            User(
                id="local-developer-user",
                org_id="local-org",
                email="developer@localhost",
                display_name="Local developer",
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
        self._seed(
            self.principals,
            "id",
            Principal(
                id="local-developer",
                org_id="local-org",
                principal_type=PrincipalType.USER,
                status=PrincipalStatus.ACTIVE,
                user_id="local-developer-user",
                created_at=now,
                updated_at=now,
            ),
        )
        self._seed(
            self.roles,
            "id",
            Role(
                id="local-developer-role",
                org_id="local-org",
                name="Local developer",
                permissions=[
                    Permission.ORGANIZATIONS_ADMIN,
                    Permission.AGENTS_MANAGE,
                    Permission.AGENTS_RUN,
                    Permission.ACTIONS_APPROVE,
                    Permission.ARTIFACTS_READ,
                    Permission.AUDIT_READ,
                    Permission.MODELS_MANAGE,
                    Permission.PLUGINS_MANAGE,
                    Permission.ROLES_MANAGE,
                    Permission.USERS_MANAGE,
                    Permission.WORKSPACES_ADMIN,
                    Permission.WORKFLOWS_MANAGE,
                    Permission.WORKFLOWS_RUN,
                ],
            ),
        )
        self._repair_developer_permissions()
        self._seed(
            self.role_bindings,
            "id",
            RoleBinding(
                id="local-developer-binding",
                org_id="local-org",
                principal_id="local-developer",
                role_id="local-developer-role",
                workspace_id="local-workspace",
                created_at=now,
            ),
        )
        developer_binding = self.role_bindings.get("local-developer-binding")
        if developer_binding.workspace_id is not None:
            self.role_bindings.put(
                developer_binding.model_copy(update={"workspace_id": None})
            )

    def _repair_developer_permissions(self) -> None:
        """Add new permissions when upgrading an existing local role."""
        developer_role = self.roles.get_scoped("local-developer-role", "local-org")
        missing = [
            permission
            for permission in Permission
            if permission not in developer_role.permissions
        ]
        if not missing:
            return
        repaired = developer_role.model_copy(
            update={"permissions": [*developer_role.permissions, *missing]}
        )
        self.roles.put(Role.model_validate(repaired.model_dump()))

    def seed_local_organization(self, now: datetime) -> None:
        """Create the default organization and workspace when absent."""
        self._seed(
            self.organizations,
            "id",
            Organization(
                id="local-org",
                name="Local development",
                status=OrganizationStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
        self._seed(
            self.workspaces,
            "id",
            Workspace(
                id="local-workspace",
                org_id="local-org",
                name="Default workspace",
                status=WorkspaceStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            ),
        )
