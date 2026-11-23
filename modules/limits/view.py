"""The read-only limits view for organization administrators."""

from __future__ import annotations

from collections.abc import Callable

from contracts.v1 import Permission
from contracts.v1.limits import OrganizationLimits
from modules.limits.service import AdmissionService
from shared.request_context import effective_org_id, effective_principal_id


class OrganizationLimitsView:
    """Report the caller's own organization's limits and consumption."""

    def __init__(
        self,
        admission: AdmissionService,
        authorize: Callable[[str, Permission], None],
    ) -> None:
        """Bind admission (for counts) and the permission check."""
        self._admission = admission
        self._authorize = authorize

    def get(self) -> OrganizationLimits:
        """Return usage against limits; organization admins only."""
        actor = effective_principal_id("local-developer")
        self._authorize(actor, Permission.ORGANIZATIONS_ADMIN)
        return self._admission.usage(effective_org_id())
