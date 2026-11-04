"""End-to-end checks for the v0 user-level RBAC contract."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from contracts.v1 import (
    Organization,
    OrganizationStatus,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
)
from modules.auth.role_presets import build_role_preset
from shared.clock import utc_now
from tests.support.runtime import LocalPlatformRuntime


def test_standard_role_bundles_and_legacy_role_payload_compatibility():
    user = build_role_preset("org-rbac", "user")
    builder = build_role_preset("org-rbac", "builder")
    admin = build_role_preset("org-rbac", "admin")
    old_role = Role.model_validate_json(
        json.dumps(
            {
                "id": "legacy-role",
                "org_id": "org-rbac",
                "name": "Legacy",
                "permissions": ["agents.run"],
            }
        )
    )

    assert user.system_role_key == "user"
    assert set(user.permissions) == {
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
    }
    assert Permission.AGENTS_MANAGE in builder.permissions
    assert Permission.WORKFLOWS_MANAGE in builder.permissions
    assert Permission.TASKS_MANAGE in builder.permissions
    assert Permission.MODELS_MANAGE not in builder.permissions
    assert set(admin.permissions) == set(Permission)
    assert old_role.system_role_key is None
    role_schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "product"
        / "v1"
        / "role.schema.json"
    )
    assert json.loads(role_schema_path.read_text()) == Role.model_json_schema()


def test_onboarding_seeds_presets_and_repeated_bootstrap_is_idempotent():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()

    before = runtime.roles.list_scoped("local-org")
    runtime.identity_bootstrap_service.seed_standard_roles()
    runtime.identity_bootstrap_service.seed_standard_roles()
    after = runtime.roles.list_scoped("local-org")

    assert len(after) == len(before)
    assert {
        role.system_role_key for role in after if role.system_role_key is not None
    } == {"admin", "builder", "user"}
    assert len([role for role in after if role.system_role_key == "admin"]) == 1


def test_legacy_owner_role_is_adopted_without_changing_its_binding():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    timestamp = utc_now()
    runtime.organizations.create(
        Organization(
            id="legacy-org",
            name="Legacy organization",
            status=OrganizationStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    legacy_permissions = [
        permission
        for permission in Permission
        if permission
        not in {
            Permission.AGENTS_VIEW,
            Permission.WORKFLOWS_VIEW,
            Permission.MODELS_VIEW,
            Permission.PLUGINS_VIEW,
            Permission.KNOWLEDGE_VIEW,
        }
    ]
    legacy_role = Role(
        id="legacy-owner-role",
        org_id="legacy-org",
        name="Organization owner",
        permissions=legacy_permissions,
    )
    legacy_binding = RoleBinding(
        id="legacy-owner-binding",
        org_id="legacy-org",
        principal_id="legacy-owner",
        role_id=legacy_role.id,
        workspace_id=None,
        created_at=timestamp,
    )
    runtime.roles.create(legacy_role)
    runtime.role_bindings.create(legacy_binding)

    runtime.identity_bootstrap_service.seed_standard_roles()

    adopted = runtime.roles.get(legacy_role.id)
    assert adopted.system_role_key == "admin"
    assert adopted.id == legacy_role.id
    assert runtime.role_bindings.get(legacy_binding.id) == legacy_binding
    assert (
        len(
            [
                role
                for role in runtime.roles.list_scoped("legacy-org")
                if role.system_role_key == "admin"
            ]
        )
        == 1
    )


def test_legacy_manage_permissions_backfill_views_without_widening_run_only_roles():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    run_only = Role(
        id="legacy-run-only",
        org_id="local-org",
        name="Legacy runner",
        permissions=[Permission.AGENTS_RUN],
    )
    workflow_runner = Role(
        id="legacy-workflow-runner",
        org_id="local-org",
        name="Legacy workflow runner",
        permissions=[Permission.WORKFLOWS_RUN],
    )
    plugin_manager = Role(
        id="legacy-plugin-manager",
        org_id="local-org",
        name="Legacy plugin manager",
        permissions=[Permission.PLUGINS_MANAGE],
    )
    runtime.roles.create(run_only)
    runtime.roles.create(workflow_runner)
    runtime.roles.create(plugin_manager)

    runtime.identity_bootstrap_service.seed_standard_roles()

    repaired_runner = runtime.roles.get(run_only.id)
    repaired_workflow_runner = runtime.roles.get(workflow_runner.id)
    repaired_plugin_manager = runtime.roles.get(plugin_manager.id)
    assert Permission.AGENTS_VIEW not in repaired_runner.permissions
    assert Permission.WORKFLOWS_VIEW in repaired_workflow_runner.permissions
    assert Permission.PLUGINS_VIEW in repaired_plugin_manager.permissions
    assert Permission.KNOWLEDGE_VIEW in repaired_plugin_manager.permissions


def test_concurrent_role_replacements_leave_one_workspace_binding():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    timestamp = datetime.now(UTC)
    principal = Principal(
        id="replacement-race-principal",
        org_id="local-org",
        principal_type=PrincipalType.SERVICE_ACCOUNT,
        status=PrincipalStatus.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )
    runtime.principals.create(principal)
    user_role = next(
        role
        for role in runtime.roles.list_scoped("local-org")
        if role.system_role_key == "user"
    )
    builder_role = next(
        role
        for role in runtime.roles.list_scoped("local-org")
        if role.system_role_key == "builder"
    )
    runtime.workspace_access_service.create_role_binding(
        principal_id=principal.id,
        role_id=user_role.id,
        workspace_id="local-workspace",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacements = (
            executor.submit(
                runtime.workspace_access_service.replace_role_binding,
                principal_id=principal.id,
                role_id=builder_role.id,
                workspace_id="local-workspace",
            ),
            executor.submit(
                runtime.workspace_access_service.replace_role_binding,
                principal_id=principal.id,
                role_id=user_role.id,
                workspace_id="local-workspace",
            ),
        )
        for replacement in replacements:
            replacement.result(timeout=5)

    assigned = [
        binding
        for binding in runtime.role_bindings.list_scoped("local-org")
        if binding.principal_id == principal.id
        and binding.workspace_id == "local-workspace"
    ]
    assert len(assigned) == 1
    assert assigned[0].role_id in {user_role.id, builder_role.id}
