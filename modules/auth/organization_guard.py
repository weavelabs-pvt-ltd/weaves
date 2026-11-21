"""One transaction boundary for ownership and changes to effective administration."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    AccountStatus,
    AuditEvent,
    MembershipStatus,
    OrganizationMembership,
    OrganizationSettings,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
    User,
    UserStatus,
)
from modules.auth.account_organization_input import AccountOrganizationCommand
from shared.request_context import effective_org_id, system_scope

OWNER_PERMISSIONS = frozenset(
    {
        Permission.ORGANIZATIONS_ADMIN,
        Permission.ROLES_MANAGE,
        Permission.USERS_MANAGE,
    }
)


class AdministrationConflict(ValueError):
    """A stale edit, unresolved legacy owner, or unsafe access change."""


class OrganizationGuard:
    """Serialize all cooperating API processes by organization, never by person."""

    def __init__(
        self,
        *,
        settings: Repository[OrganizationSettings],
        principals: Repository[Principal],
        users: Repository[User],
        roles: Repository[Role],
        bindings: Repository[RoleBinding],
        audit: Repository[AuditEvent],
        memberships: Repository[OrganizationMembership],
        accounts: Repository[Account],
        unit_of_work: Callable[[], AbstractContextManager[None]],
        lock: Callable[[str], None] | None,
    ) -> None:
        """Bind tenant-scoped persistence and authorization ports."""
        self.settings = settings
        self.principals = principals
        self.users = users
        self.roles = roles
        self.bindings = bindings
        self.audit = audit
        self.memberships = memberships
        self.accounts = accounts
        self.unit_of_work = unit_of_work
        self.lock = lock

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Keep the lock until commit/rollback; local units of work use RLocks."""
        with self.unit_of_work():
            if self.lock is not None:
                self.lock(f"weaves-org-administration:{effective_org_id()}")
            yield

    def active_humans(self) -> set[str]:
        """Ignore inactive principals/users and explicitly revoked memberships."""
        org = effective_org_id()
        active_users = {
            u.id for u in self.users.list_scoped(org) if u.status is UserStatus.ACTIVE
        }
        humans = {
            p.id: p
            for p in self.principals.list_scoped(org)
            if p.principal_type is PrincipalType.USER
            and p.status is PrincipalStatus.ACTIVE
            and p.user_id in active_users
        }
        memberships = self.memberships.list_scoped(org)
        with system_scope("account"):
            for principal_id in tuple(humans):
                links = [m for m in memberships if m.principal_id == principal_id]
                if links and not any(self._usable_membership(m) for m in links):
                    del humans[principal_id]
        return set(humans)

    def _usable_membership(self, membership: OrganizationMembership) -> bool:
        if membership.status is not MembershipStatus.ACTIVE:
            return False
        org = effective_org_id()
        if membership.org_id != org:
            return False
        try:
            principal = self.principals.get_scoped(membership.principal_id, org)
            user = self.users.get_scoped(membership.user_id, org)
            account = self.accounts.get(membership.account_id)
        except KeyError:
            return False
        return (
            principal.principal_type is PrincipalType.USER
            and principal.status is PrincipalStatus.ACTIVE
            and principal.user_id == user.id
            and user.status is UserStatus.ACTIVE
            and account.status is AccountStatus.ACTIVE
        )

    def linked_member(self, principal_id: str) -> bool:
        """A transfer recipient must already have an active verified account link."""
        if principal_id not in self.active_humans():
            return False
        links = self.memberships.list_scoped(effective_org_id())
        with system_scope("account"):
            return any(
                m.principal_id == principal_id and self._usable_membership(m)
                for m in links
            )

    def permissions(
        self, bindings: Sequence[RoleBinding]
    ) -> dict[str, set[Permission]]:
        """Only organization-wide assignments confer organization administration."""
        roles = {r.id: r for r in self.roles.list_scoped(effective_org_id())}
        result: dict[str, set[Permission]] = {}
        for binding in bindings:
            if binding.workspace_id is None and binding.role_id in roles:
                result.setdefault(binding.principal_id, set()).update(
                    roles[binding.role_id].permissions
                )
        return result

    def current_settings(self) -> OrganizationSettings:
        """Read preferences; infer legacy ownership only from its creation audit."""
        org = effective_org_id()
        try:
            return self.settings.get_scoped(org, org)
        except KeyError:
            pass
        events = [
            e
            for e in self.audit.list_scoped(org)
            if e.action == "organization.created" and e.target_id == org
        ]
        owners = {e.actor_principal_id for e in events}
        if org == "local-org" and not owners:
            owners = {
                "local-developer"
            }  # explicit local seed, never customer inference
        permissions = self.permissions(self.bindings.list_scoped(org))
        valid = self.active_humans()
        if len(owners) != 1 or not owners.issubset(valid):
            raise AdministrationConflict(
                "Organization ownership needs operator recovery"
            )
        owner = next(iter(owners))
        if not OWNER_PERMISSIONS.issubset(permissions.get(owner, set())):
            raise AdministrationConflict(
                "Organization ownership needs operator recovery"
            )
        event = min(events, key=lambda e: e.created_at) if events else None
        principal = self.principals.get_scoped(owner, org)
        command = AccountOrganizationCommand.build(
            name="Legacy organization",
            owner_email="recovery@example.invalid",
            timezone=str(event.metadata.get("timezone", "Asia/Kolkata"))
            if event
            else "Asia/Kolkata",
            locale=str(event.metadata.get("locale", "en")) if event else "en",
        )
        return OrganizationSettings(
            id=org,
            org_id=org,
            owner_principal_id=owner,
            timezone=command.timezone,
            locale=command.locale,
            created_at=event.created_at if event else principal.created_at,
            updated_at=event.created_at if event else principal.updated_at,
        )

    def validate_change(
        self,
        *,
        bindings: Sequence[RoleBinding] | None = None,
        disabled_principal: str | None = None,
    ) -> None:
        """An active owner and effective administrators must survive every mutation."""
        current = self.bindings.list_scoped(effective_org_id())
        proposed = current if bindings is None else bindings
        before = self.permissions(current)
        after = self.permissions(proposed)
        active = self.active_humans()
        remaining = active - {disabled_principal}
        owner = self.current_settings().owner_principal_id
        if owner not in remaining or not OWNER_PERMISSIONS.issubset(
            after.get(owner, set())
        ):
            raise AdministrationConflict(
                "Transfer ownership before removing the owner's access"
            )
        for permission in OWNER_PERMISSIONS:
            had_admin = any(permission in before.get(p, set()) for p in active)
            has_admin = any(permission in after.get(p, set()) for p in remaining)
            if had_admin and not has_admin:
                raise AdministrationConflict(
                    "Cannot remove the last active organization administrator"
                )
