"""Describe an account's own standing in each of its organizations.

The account views show one role pill per membership. This reader answers two
questions about the membership's own principal and nothing about anyone else:
is it the organization's explicit owner, and which role should the pill name.
It also passes on the organization's URL slug from the same settings read.

Label rule (deterministic):

1. ``Owner`` when the principal is the organization's recorded owner.
2. Otherwise the highest built-in role bound to the principal anywhere in the
   organization (organization or workspace scope): Admin, then Builder, then
   User.
3. Otherwise the first custom role name bound to it, compared case-insensitively.
4. Otherwise an empty string.

Cost: three indexed reads per listed membership (the settings row by id, the
organization's role bindings and its roles). An account's memberships are a
handful, so the listing stays bounded; the rows of other members are filtered
in memory and never returned.
"""

from __future__ import annotations

from dataclasses import dataclass

from application.ports.repository import Repository
from contracts.v1 import (
    OrganizationMembership,
    OrganizationSettings,
    Role,
    RoleBinding,
)
from modules.auth.role_presets import ROLE_PRESET_NAMES
from shared.request_context import system_scope

OWNER_LABEL = "Owner"
_BUILT_IN_ORDER = tuple(ROLE_PRESET_NAMES)  # admin, builder, user


@dataclass(frozen=True)
class MembershipRole:
    """What the account views show about one membership's standing."""

    is_owner: bool = False
    role_label: str = ""
    organization_slug: str | None = None


def role_label(roles: list[Role]) -> str:
    """Pick the label for a principal's bound roles by the documented rule."""
    by_key = {role.system_role_key: role for role in roles if role.system_role_key}
    for key in _BUILT_IN_ORDER:
        if key in by_key:
            return ROLE_PRESET_NAMES[key]
    names = sorted((role.name for role in roles), key=lambda n: (n.casefold(), n))
    return names[0] if names else ""


@dataclass(frozen=True)
class MembershipRoleReader:
    """Read ownership and role names for a membership's own principal."""

    settings: Repository[OrganizationSettings] | None = None
    roles: Repository[Role] | None = None
    bindings: Repository[RoleBinding] | None = None

    def describe(self, membership: OrganizationMembership) -> MembershipRole:
        """Return the membership's standing; unknown or unreadable is not an owner."""
        settings = self._settings(membership)
        slug = settings.slug if settings is not None else None
        if (
            settings is not None
            and settings.owner_principal_id == membership.principal_id
        ):
            return MembershipRole(True, OWNER_LABEL, slug)
        return MembershipRole(False, role_label(self._bound_roles(membership)), slug)

    def _settings(
        self, membership: OrganizationMembership
    ) -> OrganizationSettings | None:
        if self.settings is None:
            return None
        with system_scope("lifecycle"):
            try:
                return self.settings.get_scoped(membership.org_id, membership.org_id)
            except KeyError:
                return None

    def _bound_roles(self, membership: OrganizationMembership) -> list[Role]:
        if self.roles is None or self.bindings is None:
            return []
        with system_scope("auth"):
            role_ids = {
                binding.role_id
                for binding in self.bindings.list_scoped(membership.org_id)
                if binding.principal_id == membership.principal_id
            }
            if not role_ids:
                return []
            return [
                role
                for role in self.roles.list_scoped(membership.org_id)
                if role.id in role_ids
            ]
