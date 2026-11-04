"""Canonical role bundles available to every Weaves workspace."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from contracts.v1 import Permission, Role, SystemRoleKey

ROLE_PRESET_NAMES: dict[SystemRoleKey, str] = {
    "admin": "Admin",
    "builder": "Builder",
    "user": "User",
}


def role_preset_permissions(key: SystemRoleKey) -> tuple[Permission, ...]:
    """Return the server-owned permission bundle for a standard role."""
    if key == "admin":
        return tuple(Permission)
    shared_read = (
        Permission.AGENTS_VIEW,
        Permission.AGENTS_RUN,
        Permission.WORKFLOWS_VIEW,
        Permission.WORKFLOWS_RUN,
        Permission.TASKS_VIEW,
        Permission.TASKS_CREATE,
        Permission.MODELS_VIEW,
        Permission.PLUGINS_VIEW,
        Permission.KNOWLEDGE_VIEW,
        Permission.ARTIFACTS_READ,
    )
    if key == "user":
        return shared_read
    if key == "builder":
        return tuple(
            dict.fromkeys(
                (
                    *shared_read,
                    Permission.AGENTS_MANAGE,
                    Permission.WORKFLOWS_MANAGE,
                    Permission.TASKS_MANAGE,
                )
            )
        )
    raise ValueError(f"unknown system role key: {key}")


def build_role_preset(org_id: str, key: SystemRoleKey) -> Role:
    """Build a stable-ID role record from the canonical permission bundle."""
    role_id = str(uuid5(NAMESPACE_URL, f"weaves:{org_id}:role:{key}"))
    return Role(
        id=role_id,
        org_id=org_id,
        name=ROLE_PRESET_NAMES[key],
        permissions=list(role_preset_permissions(key)),
        system_role_key=key,
    )


def validate_role_preset_scope(role: Role, workspace_id: str | None) -> None:
    """Require Admin at organization scope and other presets at workspace scope."""
    if role.system_role_key == "admin" and workspace_id is not None:
        raise ValueError("the Admin role must be assigned at organization scope")
    if role.system_role_key in {"builder", "user"} and workspace_id is None:
        raise ValueError("Builder and User roles must be assigned to a workspace")
