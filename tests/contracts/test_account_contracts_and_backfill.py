"""Account contracts reject unsafe shapes; the backfill plan never merges by email."""

import pytest
from pydantic import ValidationError

from application.account_backfill import plan_account_backfill
from contracts.v1 import (
    AccountIdentity,
    AccountIdentityKind,
    OrganizationMembership,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserStatus,
)
from shared.clock import utc_now


def _identity(**changes):
    values = {
        "id": "identity-1",
        "account_id": "account-1",
        "kind": AccountIdentityKind.EMAIL,
        "subject": "person@example.test",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    return AccountIdentity(**{**values, **changes})


def test_email_identities_are_normalized_and_unverified_by_default():
    assert _identity().verified_at is None
    with pytest.raises(ValidationError, match="normalized"):
        _identity(subject="Person@Example.test")


def test_issuer_is_required_for_oidc_and_forbidden_for_email():
    with pytest.raises(ValidationError, match="issuer"):
        _identity(issuer="https://accounts.google.com")
    with pytest.raises(ValidationError, match="issuer"):
        _identity(kind=AccountIdentityKind.OIDC)
    assert _identity(
        kind=AccountIdentityKind.OIDC, issuer="https://accounts.google.com"
    ).issuer


def test_membership_requires_the_tenant_user_and_principal():
    with pytest.raises(ValidationError):
        OrganizationMembership(
            id="m",
            org_id="org",
            account_id="a",
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )  # type: ignore[call-arg]


def _user(user_id, org_id, email, status=UserStatus.ACTIVE):
    now = utc_now()
    return User(
        id=user_id,
        org_id=org_id,
        email=email,
        display_name="Person",
        status=status,
        created_at=now,
        updated_at=now,
    )


def _principal(user):
    now = utc_now()
    return Principal(
        id=f"principal-{user.id}",
        org_id=user.org_id,
        principal_type=PrincipalType.USER,
        status=PrincipalStatus.ACTIVE,
        user_id=user.id,
        created_at=now,
        updated_at=now,
    )


def test_backfill_plan_reports_shared_emails_without_merging_them():
    users = [
        _user("u1", "org-a", "shared@example.test"),
        _user("u2", "org-b", "Shared@Example.test"),
        _user("u3", "org-a", "solo@example.test"),
        _user("u4", "org-a", "gone@example.test", UserStatus.DELETED),
        _user("u5", "org-c", "twice@example.test"),
        _user("u6", "org-c", "twice@example.test"),
    ]
    report = plan_account_backfill(users, [_principal(u) for u in users])

    assert report.summary() == {
        "single_organization": 1,
        "shared_across_organizations": 1,
        "duplicate_within_organization": 1,
        "skipped_users": 1,
    }
    shared = report.shared_across_organizations[0]
    assert {member.org_id for member in shared.members} == {"org-a", "org-b"}
    assert "shared@example.test" not in repr(report)


def test_users_without_a_principal_are_skipped_not_linked():
    user = _user("u1", "org-a", "solo@example.test")
    assert plan_account_backfill([user], []).summary()["skipped_users"] == 1
