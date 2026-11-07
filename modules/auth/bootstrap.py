"""Seed the local developer identity and its platform role."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
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
from modules.auth.role_presets import build_role_preset

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
        unit_of_work: Callable[[], AbstractContextManager[None]] | None = None,
        advisory_transaction_lock: Callable[[str], None] | None = None,
    ) -> None:
        """Bind identity repositories without depending on the platform runtime."""
        self.users = users
        self.principals = principals
        self.roles = roles
        self.organizations = organizations
        self.workspaces = workspaces
        self.role_bindings = role_bindings
        self._open_unit_of_work = unit_of_work or nullcontext
        self._advisory_transaction_lock = advisory_transaction_lock

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
                    Permission.TASKS_VIEW,
                    Permission.TASKS_CREATE,
                    Permission.TASKS_MANAGE,
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

    def seed_standard_roles(self) -> None:
        """Provision or repair standard roles for every organization once."""
        with self._open_unit_of_work():
            if self._advisory_transaction_lock is not None:
                self._advisory_transaction_lock("weaves-standard-role-presets")
            for organization in self.organizations.list():
                self._ensure_organization_roles(organization.id)

    def _ensure_organization_roles(self, org_id: str) -> None:
        """Keep standard role records canonical without replacing their IDs."""
        roles = list(self.roles.list_scoped(org_id))
        bindings = self.role_bindings.list_scoped(org_id)
        admin = self._ensure_admin_role(org_id, roles, bindings)
        roles = [admin if item.id == admin.id else item for item in roles]
        for key in ("builder", "user"):
            matches = [item for item in roles if item.system_role_key == key]
            if len(matches) > 1:
                raise RuntimeError(f"organization {org_id} has duplicate {key} roles")
            if matches:
                current = matches[0]
                canonical = build_role_preset(org_id, key).model_copy(
                    update={"id": current.id}
                )
                self.roles.put(canonical)
                roles = [canonical if item.id == current.id else item for item in roles]
            else:
                created = build_role_preset(org_id, key)
                self.roles.create(created)
                roles.append(created)
        self._backfill_legacy_view_permissions(roles)

    def _ensure_admin_role(
        self, org_id: str, roles: list[Role], bindings: tuple[RoleBinding, ...]
    ) -> Role:
        """Adopt an unambiguous legacy owner role or create the Admin preset."""
        matches = [item for item in roles if item.system_role_key == "admin"]
        if len(matches) > 1:
            raise RuntimeError(f"organization {org_id} has duplicate admin roles")
        if matches:
            current = matches[0]
            canonical = build_role_preset(org_id, "admin").model_copy(
                update={"id": current.id}
            )
            self.roles.put(canonical)
            return canonical

        legacy_permissions = {
            Permission.ORGANIZATIONS_ADMIN,
            Permission.WORKSPACES_ADMIN,
            Permission.USERS_MANAGE,
            Permission.ROLES_MANAGE,
            Permission.MODELS_MANAGE,
            Permission.AGENTS_MANAGE,
            Permission.AGENTS_RUN,
            Permission.WORKFLOWS_MANAGE,
            Permission.WORKFLOWS_RUN,
            Permission.TASKS_VIEW,
            Permission.TASKS_CREATE,
            Permission.TASKS_MANAGE,
            Permission.PLUGINS_MANAGE,
            Permission.ARTIFACTS_READ,
            Permission.ACTIONS_APPROVE,
            Permission.APPROVAL_POLICIES_MANAGE,
            Permission.AUDIT_READ,
        }
        bound_role_ids = {
            binding.role_id for binding in bindings if binding.workspace_id is None
        }
        candidates = [
            role
            for role in roles
            if role.name == "Organization owner"
            and role.id in bound_role_ids
            and legacy_permissions.issubset(set(role.permissions))
        ]
        if len(candidates) > 1:
            raise RuntimeError(f"organization {org_id} has ambiguous owner roles")
        if candidates:
            current = candidates[0]
            canonical = build_role_preset(org_id, "admin").model_copy(
                update={"id": current.id}
            )
            self.roles.put(canonical)
            return canonical
        created = build_role_preset(org_id, "admin")
        self.roles.create(created)
        return created

    def _backfill_legacy_view_permissions(self, roles: list[Role]) -> None:
        """Preserve browse access for roles that already manage a module."""
        implied_views = {
            Permission.AGENTS_MANAGE: (Permission.AGENTS_VIEW,),
            Permission.WORKFLOWS_MANAGE: (Permission.WORKFLOWS_VIEW,),
            Permission.WORKFLOWS_RUN: (Permission.WORKFLOWS_VIEW,),
            Permission.TASKS_MANAGE: (Permission.TASKS_VIEW, Permission.TASKS_CREATE),
            Permission.MODELS_MANAGE: (Permission.MODELS_VIEW,),
            Permission.PLUGINS_MANAGE: (Permission.PLUGINS_VIEW,),
            Permission.SKILLS_PERSONAL_MANAGE: (Permission.SKILLS_VIEW,),
            Permission.SKILLS_ORG_PUBLISH: (Permission.SKILLS_VIEW,),
            Permission.SKILLS_ORG_MANAGE: (Permission.SKILLS_VIEW,),
        }
        for role in roles:
            permissions = set(role.permissions)
            for source, views in implied_views.items():
                if source in permissions:
                    permissions.update(views)
            if permissions == set(role.permissions):
                continue
            self.roles.put(
                role.model_copy(
                    update={
                        "permissions": [
                            permission
                            for permission in Permission
                            if permission in permissions
                        ]
                    }
                )
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
