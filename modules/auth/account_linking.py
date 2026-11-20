"""Link a verified email to the tenant users that already carry that address."""

from __future__ import annotations

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    Principal,
    PrincipalType,
    User,
    UserStatus,
)
from shared.clock import new_id, utc_now

_LINKABLE_USER_STATUSES = {UserStatus.ACTIVE, UserStatus.INVITED}


class MembershipLinker:
    """Create missing active memberships from verified-email matches only."""

    def __init__(
        self,
        *,
        users: Repository[User],
        principals: Repository[Principal],
        organizations: Repository[Organization],
        memberships: Repository[OrganizationMembership],
    ) -> None:
        """Bind the tenant directory and the global membership repository."""
        self._users = users
        self._principals = principals
        self._organizations = organizations
        self._memberships = memberships

    def link(self, account: Account, verified_email: str) -> int:
        """Return how many new memberships were created for this account.

        Existing memberships in any state are never modified, so a removed or
        suspended member is not silently restored by signing in again.
        """
        existing = self._memberships.list()
        linked_orgs = {m.org_id for m in existing if m.account_id == account.id}
        linked_users = {m.user_id for m in existing}
        created = 0
        for user in self._users.list():
            if (
                user.email.strip().casefold() != verified_email
                or user.status not in _LINKABLE_USER_STATUSES
                or user.org_id in linked_orgs
                or user.id in linked_users
            ):
                continue
            principal = self._user_principal(user)
            if principal is None or not self._organization_exists(user.org_id):
                continue
            if self._create(account, user, principal):
                linked_orgs.add(user.org_id)
                linked_users.add(user.id)
                created += 1
        return created

    def _user_principal(self, user: User) -> Principal | None:
        """Return the tenant's user-type principal for this user, if any."""
        principal = self._principals.find_by_field("user_id", user.id)
        if (
            principal is None
            or principal.principal_type is not PrincipalType.USER
            or principal.org_id != user.org_id
        ):
            return None
        return principal

    def _organization_exists(self, org_id: str) -> bool:
        """Report whether the user's organization record is still present."""
        try:
            self._organizations.get(org_id)
        except KeyError:
            return False
        return True

    def _create(self, account: Account, user: User, principal: Principal) -> bool:
        """Insert one active membership; lose quietly to a concurrent writer."""
        now = utc_now()
        membership = OrganizationMembership(
            id=new_id(),
            org_id=user.org_id,
            account_id=account.id,
            user_id=user.id,
            principal_id=principal.id,
            status=MembershipStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        try:
            self._memberships.create(membership)
        except ValueError:  # unique index: another request linked it first
            return False
        return True
