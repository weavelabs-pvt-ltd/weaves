"""API credentials, service accounts, and authorization use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    AuditEvent,
    Permission,
    Principal,
    Role,
    RoleBinding,
)
from modules.auth.runtime_credentials import CredentialRuntimeMixin
from shared.request_context import effective_org_id, effective_principal_id


class CredentialService(CredentialRuntimeMixin):
    """Authorize principals and manage service credentials with injected ports."""

    api_credentials: Repository[ApiCredential]
    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]

    def __init__(
        self,
        *,
        api_credentials: Repository[ApiCredential],
        audit_events: Repository[AuditEvent],
        principals: Repository[Principal],
        role_bindings: Repository[RoleBinding],
        roles: Repository[Role],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind credential repositories and application lifecycle callbacks."""
        self.api_credentials = api_credentials
        self.audit_events = audit_events
        self.principals = principals
        self.role_bindings = role_bindings
        self.roles = roles
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._open_unit_of_work = unit_of_work

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def list_api_credentials(self) -> tuple[ApiCredential, ...]:
        """List organization credentials newest first after authorization."""
        if not self._bootstrapped():
            self.bootstrap()
        self.authorize_organization(
            effective_principal_id("local-developer"), Permission.USERS_MANAGE
        )
        return tuple(
            sorted(
                self.api_credentials.list_scoped(effective_org_id()),
                key=lambda item: item.created_at,
                reverse=True,
            )
        )

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for credential and audit persistence."""
        return self._open_unit_of_work()
