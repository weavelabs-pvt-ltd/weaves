"""Pick the organization an account acts in, validated against current state."""

from __future__ import annotations

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    MembershipStatus,
    Organization,
    OrganizationLifecycle,
    OrganizationLifecycleState,
    OrganizationMembership,
    OrganizationSettings,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserStatus,
)
from modules.auth.lifecycle_state import effective_organization_state
from shared.request_context import system_scope

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
        lifecycle: Repository[OrganizationLifecycle] | None = None,
        settings: Repository[OrganizationSettings] | None = None,
    ) -> None:
        """Bind the global memberships and the tenant directory."""
        self._memberships = memberships
        self._users = users
        self._principals = principals
        self._organizations = organizations
        self._lifecycle = lifecycle
        self._settings = settings

    def _state(self, organization: Organization) -> OrganizationLifecycleState:
        """The effective lifecycle state, read under the narrow lifecycle scope."""
        record = None
        if self._lifecycle is not None:
            with system_scope("lifecycle"):
                try:
                    record = self._lifecycle.get(organization.id)
                except KeyError:
                    record = None
        return effective_organization_state(organization, record)

    def _is_owner(self, organization_id: str, principal_id: str) -> bool:
        if self._settings is None:
            return False
        with system_scope("lifecycle"):
            try:
                settings = self._settings.get_scoped(organization_id, organization_id)
            except KeyError:
                return False
        return settings.owner_principal_id == principal_id

    def _admitted(
        self, organization: Organization, principal_id: str, recovery: bool
    ) -> bool:
        state = self._state(organization)
        if state is OrganizationLifecycleState.ACTIVE:
            return True
        return (
            recovery
            and state is OrganizationLifecycleState.DELETION_REQUESTED
            and self._is_owner(organization.id, principal_id)
        )

    def select(
        self,
        account: Account,
        organization_id: str,
        *,
        recovery: bool = False,
    ) -> OrganizationMembership:
        """Return the active membership usable right now or raise PermissionError.

        ``recovery`` names the owner's narrow pending-deletion allow-list: only
        then, and only for the explicit owner, is DELETION_REQUESTED admitted.
        """
        try:
            membership = next(
                item
                for item in self._memberships.list_by_field("account_id", account.id)
                if item.org_id == organization_id
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
            and self._admitted(organization, principal.id, recovery)
            and user.status is UserStatus.ACTIVE
            and principal.status is PrincipalStatus.ACTIVE
            and principal.principal_type is PrincipalType.USER
            and principal.user_id == user.id
        )
        if not usable:
            raise PermissionError(_UNAVAILABLE)
        return membership
