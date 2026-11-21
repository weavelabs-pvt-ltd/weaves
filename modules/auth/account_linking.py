"""Link a verified email to the tenant users that already carry that address."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    AuditEvent,
    AuditTargetScope,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    Principal,
    PrincipalType,
    User,
    UserStatus,
    Workspace,
)
from shared.clock import new_id, utc_now
from shared.request_context import reset_request_principal, set_request_identity

_LINKABLE_USER_STATUSES = {UserStatus.ACTIVE, UserStatus.INVITED}


class _LostRace(Exception):
    """Another request created this membership first."""


class MembershipLinker:
    """Create missing active memberships from verified-email matches only."""

    def __init__(
        self,
        *,
        users: Repository[User],
        principals: Repository[Principal],
        organizations: Repository[Organization],
        memberships: Repository[OrganizationMembership],
        workspaces: Repository[Workspace],
        audit_events: Repository[AuditEvent],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind the tenant directory, the global memberships and the audit trail."""
        self._workspaces = workspaces
        self._audit_events = audit_events
        self._unit_of_work = unit_of_work
        self._users = users
        self._principals = principals
        self._organizations = organizations
        self._memberships = memberships

    def link(self, account: Account, verified_email: str) -> int:
        """Return how many new memberships were created for this account.

        Existing memberships in any state are never modified, so a removed or
        suspended member is not silently restored by signing in again.
        """
        linked_orgs = {
            m.org_id for m in self._memberships.list_by_field("account_id", account.id)
        }
        created = 0
        for user in self._users.list_by_field("email", verified_email, casefold=True):
            if (
                user.email.strip().casefold() != verified_email
                or user.status not in _LINKABLE_USER_STATUSES
                or user.org_id in linked_orgs
                or self._memberships.list_by_field("user_id", user.id)
            ):
                continue
            principal = self._user_principal(user)
            if principal is None or not self._organization_exists(user.org_id):
                continue
            if self._create(account, user, principal):
                linked_orgs.add(user.org_id)
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
            with self._unit_of_work():
                try:
                    self._memberships.create(membership)
                except ValueError as exc:  # unique index: another request won
                    raise _LostRace from exc
                self._record_link(account, membership, principal)
        except _LostRace:
            return False
        return True

    def _record_link(
        self,
        account: Account,
        membership: OrganizationMembership,
        principal: Principal,
    ) -> None:
        """Write the tenant-visible audit event inside the organization's own scope.

        Row-level security admits a tenant row only for that organization, so
        the event is written under a request identity for it and the identity
        is always reset. The email is deliberately not recorded.
        """
        workspace = next(iter(self._workspaces.list_scoped(membership.org_id)), None)
        token = set_request_identity(
            principal.id, membership.org_id, workspace.id if workspace else ""
        )
        try:
            self._audit_events.create(
                AuditEvent(
                    audit_event_id=new_id(),
                    org_id=membership.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=principal.id,
                    action="account.membership.linked",
                    target_type="membership",
                    target_id=membership.id,
                    summary="An account was linked to this member by a verified email",
                    request_id=new_id(),
                    metadata={"account_id": account.id},
                    created_at=membership.created_at,
                )
            )
        finally:
            reset_request_principal(token)
