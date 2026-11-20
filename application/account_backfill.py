"""Plan the account backfill from legacy tenant users without writing anything.

Legacy emails may be unverified and passwords differ per tenant, so matching
email strings never merge identities automatically. The plan only reports what
a verified linking flow would have to resolve.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from contracts.v1 import Principal, PrincipalType, User, UserStatus


@dataclass(frozen=True)
class TenantUserRef:
    """One tenant-local human identity that an account could be linked to."""

    org_id: str
    user_id: str
    principal_id: str
    user_status: UserStatus


@dataclass(frozen=True)
class EmailGroup:
    """All tenant users that claim the same normalized email."""

    email_digest: str
    members: tuple[TenantUserRef, ...]


@dataclass(frozen=True)
class BackfillReport:
    """Classification of legacy users; nothing here has been linked or merged."""

    single_organization: tuple[EmailGroup, ...]
    shared_across_organizations: tuple[EmailGroup, ...]
    duplicate_within_organization: tuple[EmailGroup, ...]
    skipped_users: int

    def summary(self) -> dict[str, int]:
        """Counts only, safe to print: no addresses, names or identifiers."""
        return {
            "single_organization": len(self.single_organization),
            "shared_across_organizations": len(self.shared_across_organizations),
            "duplicate_within_organization": len(self.duplicate_within_organization),
            "skipped_users": self.skipped_users,
        }


def plan_account_backfill(
    users: Iterable[User], principals: Iterable[Principal]
) -> BackfillReport:
    """Group legacy users by email without claiming any of them is the same person."""
    principal_by_user = {
        principal.user_id: principal
        for principal in principals
        if principal.principal_type is PrincipalType.USER and principal.user_id
    }
    groups: dict[str, list[TenantUserRef]] = defaultdict(list)
    skipped = 0
    for user in users:
        principal = principal_by_user.get(user.id)
        if user.status is UserStatus.DELETED or principal is None:
            skipped += 1
            continue
        groups[user.email.strip().casefold()].append(
            TenantUserRef(user.org_id, user.id, principal.id, user.status)
        )
    single, shared, duplicate = [], [], []
    for email, members in sorted(groups.items()):
        digest = hashlib.sha256(
            f"weaves-account-backfill\0{email}".encode()
        ).hexdigest()
        group = EmailGroup(digest[:16], tuple(members))
        organizations = [member.org_id for member in members]
        if len(organizations) != len(set(organizations)):
            duplicate.append(group)
        elif len(members) > 1:
            shared.append(group)
        else:
            single.append(group)
    return BackfillReport(tuple(single), tuple(shared), tuple(duplicate), skipped)
