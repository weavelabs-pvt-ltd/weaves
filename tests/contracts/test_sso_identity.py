"""Tenant isolation and explicit subject binding for enterprise SSO."""

from contextlib import nullcontext
from datetime import UTC, datetime

import pytest

from contracts.v1 import (
    Principal,
    PrincipalStatus,
    PrincipalType,
    SsoConnection,
    SsoConnectionStatus,
    SsoProvider,
    User,
    UserStatus,
)
from infrastructure.persistence.memory_repository import InMemoryRepository
from modules.auth.sso_identity_service import SsoIdentityService


def _fixture():
    now = datetime.now(UTC)
    connections = InMemoryRepository("id")
    identities = InMemoryRepository("id")
    users = InMemoryRepository("id")
    principals = InMemoryRepository("id")
    audits = InMemoryRepository("audit_event_id")
    for org_id in ("org-a", "org-b"):
        users.create(
            User(
                id=f"user-{org_id}",
                org_id=org_id,
                email="same@example.com",
                display_name="Same Name",
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        principals.create(
            Principal(
                id=f"principal-{org_id}",
                org_id=org_id,
                principal_type=PrincipalType.USER,
                status=PrincipalStatus.ACTIVE,
                user_id=f"user-{org_id}",
                created_at=now,
                updated_at=now,
            )
        )
        connections.create(
            SsoConnection(
                id=f"connection-{org_id}",
                org_id=org_id,
                provider=SsoProvider.GOOGLE,
                status=SsoConnectionStatus.ENABLED,
                issuer="https://accounts.google.com",
                client_id="test-client",
                client_secret_ref="secret:google",
                created_at=now,
                updated_at=now,
            )
        )
    service = SsoIdentityService(
        connections=connections,
        identities=identities,
        users=users,
        principals=principals,
        audit_events=audits,
        authorize_organization=lambda actor, permission: None,
        unit_of_work=nullcontext,
    )
    return service, users, principals, audits


def _bind(service, org_id="org-a", subject="provider-subject", user_id=None):
    return service.bind_existing_user(
        org_id=org_id,
        connection_id=f"connection-{org_id}",
        issuer="https://accounts.google.com",
        subject=subject,
        user_id=user_id or f"user-{org_id}",
        requested_by_principal_id=f"principal-{org_id}",
    )


def test_same_email_in_two_organizations_never_links_accounts():
    service, _, _, audits = _fixture()
    _bind(service, "org-a")
    assert (
        service.resolve_user(
            org_id="org-a",
            connection_id="connection-org-a",
            issuer="https://accounts.google.com",
            subject="provider-subject",
        ).id
        == "user-org-a"
    )
    with pytest.raises(PermissionError, match="not linked"):
        service.resolve_user(
            org_id="org-b",
            connection_id="connection-org-b",
            issuer="https://accounts.google.com",
            subject="provider-subject",
        )
    _bind(service, "org-b")
    assert (
        service.resolve_user(
            org_id="org-b",
            connection_id="connection-org-b",
            issuer="https://accounts.google.com",
            subject="provider-subject",
        ).id
        == "user-org-b"
    )
    assert len(audits.list()) == 2


def test_binding_rejects_duplicate_subject_user_and_cross_tenant_actor():
    service, _, _, _ = _fixture()
    _bind(service)
    with pytest.raises(ValueError, match="already linked"):
        _bind(service)
    with pytest.raises(ValueError, match="already linked"):
        _bind(service, subject="different-subject")
    with pytest.raises(KeyError):
        service.bind_existing_user(
            org_id="org-a",
            connection_id="connection-org-a",
            issuer="https://accounts.google.com",
            subject="new-subject",
            user_id="user-org-a",
            requested_by_principal_id="principal-org-b",
        )


def test_binding_and_resolution_reject_wrong_issuer_and_disabled_user():
    service, users, _, _ = _fixture()
    with pytest.raises(PermissionError, match="does not match"):
        service.bind_existing_user(
            org_id="org-a",
            connection_id="connection-org-a",
            issuer="https://attacker.example",
            subject="subject",
            user_id="user-org-a",
            requested_by_principal_id="principal-org-a",
        )
    _bind(service)
    with pytest.raises(PermissionError, match="does not match"):
        service.resolve_user(
            org_id="org-a",
            connection_id="connection-org-a",
            issuer="https://attacker.example",
            subject="provider-subject",
        )
    user = users.get("user-org-a")
    users.put(user.model_copy(update={"status": UserStatus.SUSPENDED}))
    with pytest.raises(PermissionError, match="disabled"):
        service.resolve_user(
            org_id="org-a",
            connection_id="connection-org-a",
            issuer="https://accounts.google.com",
            subject="provider-subject",
        )
