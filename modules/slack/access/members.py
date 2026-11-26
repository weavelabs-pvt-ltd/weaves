"""Read a mapped Weaves member's standing from the organization's own records."""

from __future__ import annotations

from modules.auth.organization_guard import OrganizationGuard
from shared.request_context import effective_org_id, effective_workspace_id


class MemberDirectory:
    """Active-member, name and role-permission lookups for allowlist mapping.

    The Slack user to member mapping is administrator-asserted; nothing here
    matches by email or lets a Slack user claim a member.
    """

    def __init__(self, guard: OrganizationGuard) -> None:
        """Bind the organization guard that already owns these repositories."""
        self._guard = guard

    def is_active(self, principal_id: str) -> bool:
        """An active human with a usable account link in this organization."""
        return self._guard.linked_member(principal_id)

    def display_name(self, principal_id: str) -> str:
        """The member's display name, or an empty string if unknown."""
        org = effective_org_id()
        try:
            principal = self._guard.principals.get_scoped(principal_id, org)
            return self._guard.users.get_scoped(
                principal.user_id or "", org
            ).display_name
        except KeyError:
            return ""

    def permissions(self, principal_id: str) -> frozenset[str]:
        """Permissions from organization-wide and current-workspace role bindings."""
        org = effective_org_id()
        workspace = effective_workspace_id()
        roles = {role.id: role for role in self._guard.roles.list_scoped(org)}
        granted: set[str] = set()
        for binding in self._guard.bindings.list_scoped(org):
            if binding.principal_id != principal_id:
                continue
            if binding.workspace_id not in (None, workspace):
                continue
            role = roles.get(binding.role_id)
            if role is not None:
                granted.update(permission.value for permission in role.permissions)
        return frozenset(granted)
