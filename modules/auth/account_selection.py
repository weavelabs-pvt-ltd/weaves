"""Pick the organization an account acts in, validated against current state."""

from __future__ import annotations

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    OrganizationStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserStatus,
)

_UNAVAILABLE = "organization is not available"


class AccountOrganizationSelector:
    """Resolve an account's membership in one organization for this request.

    Every refusal uses one error, so an unknown organization, a missing
    membership, a removed or suspended membership, a disabled user and a
    suspended organization are indistinguishable to the caller.
    """

    def __init__(
        self,
        *,
        memberships: Repository[OrganizationMembership],
        users: Repository[User],
        principals: Repository[Principal],
        organizations: Repository[Organization],
    ) -> None:
        """Bind the global memberships and the tenant directory."""
        self._memberships = memberships
        self._users = users
        self._principals = principals
        self._organizations = organizations

    def select(self, account: Account, organization_id: str) -> OrganizationMembership:
        """Return the active membership usable right now or raise PermissionError."""
        try:
            membership = next(
                item
                for item in self._memberships.list_scoped(organization_id)
                if item.account_id == account.id
            )
            organization = self._organizations.get(organization_id)
            user = self._users.get_scoped(membership.user_id, organization_id)
            principal = self._principals.get_scoped(
                membership.principal_id, organization_id
            )
        except (StopIteration, KeyError):
            raise PermissionError(_UNAVAILABLE) from None
        usable = (
            membership.status is MembershipStatus.ACTIVE
            and organization.status is OrganizationStatus.ACTIVE
            and user.status is UserStatus.ACTIVE
            and principal.status is PrincipalStatus.ACTIVE
            and principal.principal_type is PrincipalType.USER
            and principal.user_id == user.id
        )
        if not usable:
            raise PermissionError(_UNAVAILABLE)
        return membership
