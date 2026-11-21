"""Seed tenant organizations and users for account sign-in tests."""

from __future__ import annotations

from contracts.v1 import (
    Organization,
    OrganizationStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserStatus,
)
from shared.clock import utc_now


def seed_organization(organizations, org_id, status=OrganizationStatus.ACTIVE):
    """Create one organization record."""
    now = utc_now()
    organizations.create(
        Organization(
            id=org_id,
            name=f"Org {org_id}",
            status=status,
            created_at=now,
            updated_at=now,
        )
    )


def seed_member(
    users,
    principals,
    org_id,
    email,
    *,
    user_id=None,
    status=UserStatus.ACTIVE,
    principal_type=PrincipalType.USER,
):
    """Create a tenant user and its principal; return (user, principal)."""
    now = utc_now()
    user_id = user_id or f"user-{org_id}-{len(users.list())}"
    user = User(
        id=user_id,
        org_id=org_id,
        email=email,
        display_name="Member",
        status=status,
        created_at=now,
        updated_at=now,
    )
    is_user = principal_type is PrincipalType.USER
    principal = Principal(
        id=f"principal-{user_id}",
        org_id=org_id,
        principal_type=principal_type,
        status=PrincipalStatus.ACTIVE,
        user_id=user_id if is_user else None,
        created_at=now,
        updated_at=now,
    )
    users.create(user)
    principals.create(principal)
    return user, principal
