"""Read-only account views: masked identities and the account's memberships."""

from __future__ import annotations

from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    MembershipStatus,
    Organization,
    OrganizationMembership,
)

_LISTED_STATUSES = {MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED}


def mask_email(email: str) -> str:
    """Show enough of an address to recognize it: p***@example.com."""
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def list_memberships(
    account: Account,
    memberships: Repository[OrganizationMembership],
    organizations: Repository[Organization],
) -> list[dict[str, Any]]:
    """List only this account's active or suspended memberships."""
    rows: list[dict[str, Any]] = []
    for membership in sorted(memberships.list(), key=lambda m: m.created_at):
        if membership.account_id != account.id:
            continue
        if membership.status not in _LISTED_STATUSES:
            continue
        try:
            organization = organizations.get(membership.org_id)
        except KeyError:
            continue
        rows.append(
            {
                "organization_id": organization.id,
                "organization_name": organization.name,
                "organization_status": organization.status.value,
                "membership_status": membership.status.value,
                "created_at": membership.created_at,
            }
        )
    return rows


def list_identities(
    account: Account, identities: Repository[AccountIdentity]
) -> list[dict[str, Any]]:
    """List this account's verified identities with the address masked."""
    return [
        {
            "id": identity.id,
            "kind": identity.kind.value,
            "subject": (
                mask_email(identity.subject)
                if identity.kind is AccountIdentityKind.EMAIL
                else "***"
            ),
            "verified_at": identity.verified_at,
        }
        for identity in identities.list()
        if identity.account_id == account.id and identity.verified_at is not None
    ]


def account_summary(account: Account) -> dict[str, Any]:
    """The only account fields clients receive."""
    return {"id": account.id, "display_name": account.display_name}
