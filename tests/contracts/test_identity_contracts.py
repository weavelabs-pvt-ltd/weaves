from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    EmailLoginChallenge,
    Organization,
    OrganizationStatus,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    User,
    UserSession,
    UserSessionStatus,
    Workspace,
    WorkspaceStatus,
)

NOW = datetime.now(UTC)


def test_api_credential_contract_persists_digest_and_revocation_state():
    credential = ApiCredential(
        id="credential-a",
        token_digest="a" * 64,
        org_id="org-a",
        principal_id="principal-a",
        status=ApiCredentialStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    assert credential.token_digest == "a" * 64
    with pytest.raises(ValidationError):
        ApiCredential(
            id="credential-b",
            token_digest="not-a-digest",
            org_id="org-a",
            principal_id="principal-a",
            status=ApiCredentialStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )


def test_user_session_contract_requires_digest_and_consistent_revocation():
    session = UserSession(
        id="session-a",
        org_id="org-a",
        principal_id="principal-a",
        token_digest="b" * 64,
        status=UserSessionStatus.ACTIVE,
        expires_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    assert session.token_digest == "b" * 64
    with pytest.raises(ValidationError):
        UserSession(
            id="session-b",
            org_id="org-a",
            principal_id="principal-a",
            token_digest="invalid",
            status=UserSessionStatus.ACTIVE,
            expires_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )


def test_email_login_challenge_rejects_non_digest_codes():
    with pytest.raises(ValidationError):
        EmailLoginChallenge(
            challenge_id="challenge-a",
            org_id="org-a",
            user_id="user-a",
            principal_id="principal-a",
            email="person@example.com",
            code_digest="123456",
            expires_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValidationError):
        UserSession(
            id="session-c",
            org_id="org-a",
            principal_id="principal-a",
            token_digest="c" * 64,
            status=UserSessionStatus.REVOKED,
            expires_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )


def test_product_contracts_reject_unknown_fields():
    with pytest.raises(ValidationError):
        Organization(
            id="org-a",
            name="Example",
            status=OrganizationStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            surprise="no",
        )


@pytest.mark.parametrize("value", ["", "   ", "x" * 129])
def test_ids_must_be_non_empty_and_bounded(value):
    with pytest.raises(ValidationError):
        Workspace(
            id=value,
            org_id="org-a",
            name="Default",
            status=WorkspaceStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )


def test_timestamps_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        Organization(
            id="org-a",
            name="Example",
            status=OrganizationStatus.ACTIVE,
            created_at=datetime(2026, 1, 1),
            updated_at=NOW,
        )


def test_workspace_has_explicit_organization_scope():
    with pytest.raises(ValidationError):
        Workspace(
            id="ws-a",
            name="Default",
            status=WorkspaceStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )


def test_closed_status_values_are_enforced():
    with pytest.raises(ValidationError):
        User(
            id="user-a",
            org_id="org-a",
            email="person@example.com",
            display_name="Person",
            status="enabled",
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValidationError):
        Principal(
            id="principal-a",
            org_id="org-a",
            principal_type=PrincipalType.SYSTEM,
            status="enabled",
            created_at=NOW,
            updated_at=NOW,
        )


def test_user_principals_require_matching_user_reference():
    with pytest.raises(ValidationError):
        Principal(
            id="principal-a",
            org_id="org-a",
            principal_type=PrincipalType.USER,
            status=PrincipalStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValidationError):
        Principal(
            id="principal-b",
            org_id="org-a",
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            status=PrincipalStatus.ACTIVE,
            user_id="user-a",
            created_at=NOW,
            updated_at=NOW,
        )


def test_role_binding_keeps_both_tenant_scopes():
    binding = RoleBinding(
        id="binding-a",
        org_id="org-a",
        workspace_id="workspace-a",
        principal_id="principal-a",
        role_id="role-a",
        created_at=NOW,
    )
    assert binding.org_id == "org-a"
    assert binding.workspace_id == "workspace-a"


def test_organization_role_binding_uses_none_for_workspace_scope():
    binding = RoleBinding(
        id="binding-a",
        org_id="org-a",
        workspace_id=None,
        principal_id="principal-a",
        role_id="role-a",
        created_at=NOW,
    )
    assert binding.workspace_id is None
    with pytest.raises(ValidationError):
        RoleBinding(
            id="binding-b",
            org_id="org-a",
            workspace_id="",
            principal_id="principal-a",
            role_id="role-a",
            created_at=NOW,
        )


def test_role_permission_values_are_closed_and_unique():
    role = Role(
        id="role-a",
        org_id="org-a",
        name="Workspace admin",
        permissions=[Permission.AGENTS_RUN, Permission.AGENTS_MANAGE],
    )
    assert Permission.AGENTS_RUN in role.permissions
    with pytest.raises(ValidationError):
        Role(
            id="role-b",
            org_id="org-a",
            name="Bad role",
            permissions=["unknown.permission"],
        )
    with pytest.raises(ValidationError):
        Role(
            id="role-c",
            org_id="org-a",
            name="Duplicate role",
            permissions=[Permission.AGENTS_RUN, Permission.AGENTS_RUN],
        )
