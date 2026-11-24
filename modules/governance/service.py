"""Governance use cases: the audit trail's read side."""

from __future__ import annotations

from collections.abc import Callable

from application.ports.repository import Repository
from contracts.v1 import AuditEvent, Permission
from modules.governance.audit_search import GovernanceAuditMixin


class GovernanceService(GovernanceAuditMixin):
    """Coordinate audit search over the recorded audit stream."""

    audit_events: Repository[AuditEvent]

    def __init__(
        self,
        *,
        audit_events: Repository[AuditEvent],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authorize: Callable[[str, Permission], None],
    ) -> None:
        """Bind governance ports supplied by the process composition root."""
        self.audit_events = audit_events
        self._is_bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authorize = authorize

    def bootstrap(self) -> None:
        """Delegate bootstrap to the application lifecycle owner."""
        self._bootstrap()

    def _ensure_bootstrapped(self) -> None:
        """Bootstrap built-in records when the application has not done so."""
        if not self._is_bootstrapped():
            self.bootstrap()

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Delegate authorization to the configured identity capability."""
        self._authorize(principal_id, permission)
