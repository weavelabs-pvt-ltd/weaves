"""Tenant preferences, explicit ownership transfer and membership removal."""

from collections.abc import Callable
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    AuditEvent,
    AuditTargetScope,
    MembershipStatus,
    Organization,
    Permission,
    PrincipalStatus,
    RoleBinding,
    User,
    UserStatus,
)
from modules.auth.account_organization_input import AccountOrganizationCommand
from modules.auth.organization_guard import AdministrationConflict, OrganizationGuard
from modules.auth.organization_slugs import OrganizationSlugs, SlugRejected
from modules.auth.role_presets import build_role_preset
from shared.clock import new_id, utc_now
from shared.request_context import effective_org_id, effective_principal_id


class OrganizationAdministrationService:
    """All writes share the same guard as role and user lifecycle mutations."""

    def __init__(
        self,
        *,
        guard: OrganizationGuard,
        organizations: Repository[Organization],
        credentials: Repository[ApiCredential],
        authorize: Callable[[str, Permission], None],
        slugs: OrganizationSlugs,
    ) -> None:
        """Bind tenant-scoped persistence and authorization ports."""
        self.slugs = slugs
        self.guard = guard
        self.organizations = organizations
        self.credentials = credentials
        self.authorize = authorize

    def _authorize(
        self, permission: Permission = Permission.ORGANIZATIONS_ADMIN
    ) -> str:
        actor = effective_principal_id("local-developer")
        self.authorize(actor, permission)
        return actor

    def get(self) -> dict[str, Any]:
        """Return only the selected organization's administration view."""
        actor = self._authorize()
        org_id = effective_org_id()
        organization = self.organizations.get(org_id)
        settings = self.guard.current_settings()
        owner = self.guard.principals.get_scoped(settings.owner_principal_id, org_id)
        user = self.guard.users.get_scoped(owner.user_id or "", org_id)
        is_owner = actor == owner.id
        candidates = []
        if is_owner:
            for principal in self.guard.principals.list_scoped(org_id):
                if principal.id != owner.id and self.guard.linked_member(principal.id):
                    member = self.guard.users.get_scoped(
                        principal.user_id or "", org_id
                    )
                    candidates.append(self._member(member, principal.id))
        return {
            "organization": organization.model_dump(mode="json"),
            "settings": settings.model_dump(mode="json"),
            "owner": self._member(user, owner.id),
            "can_transfer": is_owner,
            "transfer_candidates": sorted(candidates, key=lambda m: m["display_name"]),
        }

    @staticmethod
    def _member(user: User, principal_id: str) -> dict[str, str]:
        return {
            "user_id": user.id,
            "principal_id": principal_id,
            "display_name": user.display_name,
            "email": user.email,
        }

    def update(
        self,
        *,
        name: str,
        timezone: str,
        locale: str,
        expected_revision: int,
        slug: str | None = None,
    ) -> dict[str, Any]:
        """Commit preferences and audit together, rejecting stale submissions.

        ``slug`` is optional: omitted keeps the current one. A malformed or
        reserved slug raises ``SlugRejected``; one held by another organization
        raises ``SlugTaken``. The email domain is read-only here.
        """
        try:
            command = AccountOrganizationCommand.build(
                name=name,
                timezone=timezone,
                locale=locale,
                owner_email="settings@example.invalid",
                slug=slug,
            )
        except SlugRejected as exc:
            raise self.slugs.explain(exc, effective_org_id()) from exc
        with self.guard.mutation():
            actor = self._authorize()
            settings = self.guard.current_settings()
            self._revision(settings.revision, expected_revision)
            organization = self.organizations.get(effective_org_id())
            before = {
                "name": organization.name,
                "timezone": settings.timezone,
                "locale": settings.locale,
                "slug": settings.slug,
            }
            after = {
                "name": command.name,
                "timezone": command.timezone,
                "locale": command.locale,
                "slug": command.slug or settings.slug,
            }
            if before == after:
                return self.get()
            if after["slug"] != before["slug"]:
                self.slugs.reserve(str(after["slug"]), organization.id)
            now = utc_now()
            self.organizations.put(
                organization.model_copy(
                    update={"name": command.name, "updated_at": now}
                )
            )
            self.guard.settings.put(
                settings.model_copy(
                    update={
                        "timezone": command.timezone,
                        "locale": command.locale,
                        "slug": after["slug"],
                        "revision": settings.revision + 1,
                        "updated_at": now,
                    }
                )
            )
            self._audit(
                actor,
                "organization.settings_updated",
                "Organization settings updated",
                {"before": before, "after": after},
            )
            return self.get()

    @staticmethod
    def _revision(actual: int, expected: int) -> None:
        if actual != expected:
            raise AdministrationConflict(
                "Organization settings changed. Reload before trying again"
            )

    def transfer(
        self,
        *,
        principal_id: str,
        expected_revision: int,
        confirmation: str,
    ) -> dict[str, Any]:
        """Transfer to a signed-in active member; the former owner remains Admin."""
        with self.guard.mutation():
            actor = self._authorize()
            settings = self.guard.current_settings()
            self._revision(settings.revision, expected_revision)
            if actor != settings.owner_principal_id:
                raise PermissionError("Only the current owner can transfer ownership")
            organization = self.organizations.get(effective_org_id())
            if confirmation != organization.name:
                raise ValueError("Confirm the organization's current name")
            if principal_id == actor:
                raise ValueError("Choose a different organization member")
            self.guard.principals.get_scoped(principal_id, effective_org_id())
            if not self.guard.linked_member(principal_id):
                raise AdministrationConflict(
                    "Choose an active member who has signed in"
                )
            admin = build_role_preset(effective_org_id(), "admin")
            self.guard.roles.put(admin)
            bindings = self.guard.bindings.list_scoped(effective_org_id())
            if not any(
                b.principal_id == principal_id
                and b.workspace_id is None
                and b.role_id == admin.id
                for b in bindings
            ):
                self.guard.bindings.create(
                    RoleBinding(
                        id=new_id(),
                        org_id=effective_org_id(),
                        principal_id=principal_id,
                        role_id=admin.id,
                        workspace_id=None,
                        created_at=utc_now(),
                    )
                )
            self.guard.settings.put(
                settings.model_copy(
                    update={
                        "owner_principal_id": principal_id,
                        "revision": settings.revision + 1,
                        "updated_at": utc_now(),
                    }
                )
            )
            self.guard.validate_change()
            self._audit(
                actor,
                "organization.ownership_transferred",
                "Organization ownership transferred",
                {
                    "previous_owner_principal_id": actor,
                    "owner_principal_id": principal_id,
                },
            )
            return self.get()

    def remove_member(self, user_id: str) -> None:
        """Remove tenant access, preserving the global account and history."""
        with self.guard.mutation():
            actor = self._authorize(Permission.USERS_MANAGE)
            org = effective_org_id()
            user = self.guard.users.get_scoped(user_id, org)
            principals = self.guard.principals.list_by_field("user_id", user.id)
            principal = next((p for p in principals if p.org_id == org), None)
            if principal is None:
                raise KeyError("Member not found")
            if user.status is UserStatus.DELETED:
                return
            self.guard.validate_change(disabled_principal=principal.id)
            now = utc_now()
            self.guard.users.put(
                user.model_copy(
                    update={"status": UserStatus.DELETED, "updated_at": now}
                )
            )
            self.guard.principals.put(
                principal.model_copy(
                    update={"status": PrincipalStatus.DISABLED, "updated_at": now}
                )
            )
            for link in self.guard.memberships.list_scoped(org):
                if link.user_id == user.id:
                    self.guard.memberships.put(
                        link.model_copy(
                            update={
                                "status": MembershipStatus.REMOVED,
                                "updated_at": now,
                            }
                        )
                    )
            for credential in self.credentials.list_scoped(org):
                if (
                    credential.principal_id == principal.id
                    and credential.status is ApiCredentialStatus.ACTIVE
                ):
                    self.credentials.put(
                        credential.model_copy(
                            update={
                                "status": ApiCredentialStatus.REVOKED,
                                "revoked_at": now,
                                "updated_at": now,
                            }
                        )
                    )
            self._audit(
                actor,
                "organization.member_removed",
                "Member access removed from organization",
                {"user_id": user.id, "principal_id": principal.id},
            )

    def _audit(
        self, actor: str, action: str, summary: str, metadata: dict[str, Any]
    ) -> None:
        self.guard.audit.create(
            AuditEvent(
                audit_event_id=new_id(),
                org_id=effective_org_id(),
                target_scope=AuditTargetScope.ORGANIZATION,
                actor_principal_id=actor,
                action=action,
                target_type="organization",
                target_id=effective_org_id(),
                summary=summary,
                request_id=new_id(),
                metadata=metadata,
                created_at=utc_now(),
            )
        )
