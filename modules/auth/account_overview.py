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
    OrganizationLifecycle,
    OrganizationLifecycleState,
    OrganizationMembership,
    OrganizationStatus,
)
from modules.auth.lifecycle_state import effective_organization_state
from shared.request_context import system_scope

_LISTED_STATUSES = {MembershipStatus.ACTIVE, MembershipStatus.SUSPENDED}


def mask_email(email: str) -> str:
    """Show enough of an address to recognize it: p***@example.com."""
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


def _record(
    organization: Organization, lifecycle: Repository[OrganizationLifecycle] | None
) -> OrganizationLifecycle | None:
    if lifecycle is None:
        return None
    with system_scope("lifecycle"):
        try:
            return lifecycle.get(organization.id)
        except KeyError:
            return None


def _state(
    organization: Organization, lifecycle: Repository[OrganizationLifecycle] | None
) -> OrganizationLifecycleState:
    return effective_organization_state(organization, _record(organization, lifecycle))


def _scheduled(
    state: OrganizationLifecycleState,
    organization: Organization,
    lifecycle: Repository[OrganizationLifecycle] | None,
) -> Any:
    if state is not OrganizationLifecycleState.DELETION_REQUESTED:
        return None
    record = _record(organization, lifecycle)
    return record.grace_deadline if record else None


def list_memberships(
    account: Account,
    memberships: Repository[OrganizationMembership],
    organizations: Repository[Organization],
    lifecycle: Repository[OrganizationLifecycle] | None = None,
) -> list[dict[str, Any]]:
    """List only this account's active or suspended memberships."""
    rows: list[dict[str, Any]] = []
    owned = memberships.list_by_field("account_id", account.id)
    for membership in sorted(owned, key=lambda m: m.created_at):
        if membership.status not in _LISTED_STATUSES:
            continue
        try:
            organization = organizations.get(membership.org_id)
        except KeyError:
            continue
        if organization.status is OrganizationStatus.DELETED:
            continue
        state = _state(organization, lifecycle)
        rows.append(
            {
                "organization_id": organization.id,
                "organization_name": organization.name,
                "organization_status": organization.status.value,
                "lifecycle_state": state.value,
                "deletion_scheduled_for": _scheduled(state, organization, lifecycle),
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
        for identity in identities.list_by_field("account_id", account.id)
        if identity.verified_at is not None
    ]


def account_summary(account: Account) -> dict[str, Any]:
    """The only account fields clients receive."""
    return {"id": account.id, "display_name": account.display_name}
