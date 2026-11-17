"""Admin-controlled binding of provider subjects to existing tenant users."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    SsoConnection,
    SsoConnectionStatus,
    SsoExternalIdentity,
    User,
    UserStatus,
)
from shared.clock import new_id, utc_now


class SsoIdentityService:
    """Never derive a Weaves account from an IdP email address."""

    def __init__(
        self,
        *,
        connections: Repository[SsoConnection],
        identities: Repository[SsoExternalIdentity],
        users: Repository[User],
        principals: Repository[Principal],
        audit_events: Repository[AuditEvent],
        authorize_organization: Callable[[str, Permission], None],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind repositories, authorization, and transaction boundaries."""
        self.connections = connections
        self.identities = identities
        self.users = users
        self.principals = principals
        self.audit_events = audit_events
        self.authorize_organization = authorize_organization
        self.unit_of_work = unit_of_work

    def bind_existing_user(
        self,
        *,
        org_id: str,
        connection_id: str,
        issuer: str,
        subject: str,
        user_id: str,
        requested_by_principal_id: str,
    ) -> SsoExternalIdentity:
        """Bind a verified OIDC subject after explicit organization admin approval."""
        with self.unit_of_work():
            self._validate_binding(
                org_id,
                connection_id,
                issuer,
                subject,
                user_id,
                requested_by_principal_id,
            )
            timestamp = utc_now()
            identity = SsoExternalIdentity(
                id=new_id(),
                org_id=org_id,
                connection_id=connection_id,
                issuer=issuer,
                subject=subject,
                user_id=user_id,
                created_by_principal_id=requested_by_principal_id,
                created_at=timestamp,
            )
            self.identities.create(identity)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=new_id(),
                    org_id=org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=requested_by_principal_id,
                    action="sso.identity_linked",
                    target_type="sso_external_identity",
                    target_id=identity.id,
                    summary="Administrator linked an existing user to an SSO identity",
                    request_id=identity.id,
                    metadata={"connection_id": connection_id, "user_id": user_id},
                    created_at=timestamp,
                )
            )
        return identity

    def _validate_binding(
        self,
        org_id: str,
        connection_id: str,
        issuer: str,
        subject: str,
        user_id: str,
        requested_by_principal_id: str,
    ) -> None:
        """Check tenant access and uniqueness inside the write transaction."""
        self.authorize_organization(
            requested_by_principal_id, Permission.ORGANIZATIONS_ADMIN
        )
        actor = self.principals.get_scoped(requested_by_principal_id, org_id)
        if (
            actor.status is not PrincipalStatus.ACTIVE
            or actor.principal_type is not PrincipalType.USER
        ):
            raise PermissionError("administrator is disabled")
        if actor.user_id is None:
            raise PermissionError("administrator has no user account")
        self._active_user(org_id, actor.user_id)
        connection = self.connections.get_scoped(connection_id, org_id)
        if connection.status not in {
            SsoConnectionStatus.TESTED,
            SsoConnectionStatus.ENABLED,
        }:
            raise PermissionError("SSO connection has not been tested")
        if issuer != connection.issuer or not subject.strip():
            raise PermissionError("provider identity does not match the connection")
        self._active_user(org_id, user_id)
        for existing in self.identities.list_scoped(org_id):
            if existing.connection_id != connection_id:
                continue
            if existing.issuer == issuer and existing.subject == subject:
                raise ValueError("provider identity is already linked")
            if existing.user_id == user_id:
                raise ValueError("user is already linked to this connection")

    def resolve_user(
        self, *, org_id: str, connection_id: str, issuer: str, subject: str
    ) -> User:
        """Resolve only a previously bound identity in the selected tenant."""
        connection = self.connections.get_scoped(connection_id, org_id)
        if connection.status is not SsoConnectionStatus.ENABLED:
            raise PermissionError("SSO connection is disabled")
        if issuer != connection.issuer or not subject:
            raise PermissionError("provider identity does not match the connection")
        matches = [
            identity
            for identity in self.identities.list_scoped(org_id)
            if identity.connection_id == connection_id
            and identity.issuer == issuer
            and identity.subject == subject
        ]
        if len(matches) != 1:
            raise PermissionError("provider identity is not linked")
        return self._active_user(org_id, matches[0].user_id)

    def _active_user(self, org_id: str, user_id: str) -> User:
        """Share account/principal eligibility across linking and resolution."""
        user = self.users.get_scoped(user_id, org_id)
        if user.status is not UserStatus.ACTIVE:
            raise PermissionError("user account is disabled")
        principal = next(
            (
                item
                for item in self.principals.list_scoped(org_id)
                if item.user_id == user.id and item.status is PrincipalStatus.ACTIVE
            ),
            None,
        )
        if principal is None:
            raise PermissionError("user principal is disabled")
        return user
