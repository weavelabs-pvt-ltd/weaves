"""API credential, service account, and permission authorization use cases."""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    AuditEvent,
    AuditTargetScope,
    Permission,
    Principal,
    PrincipalStatus,
    PrincipalType,
    Role,
    RoleBinding,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import (
    effective_org_id,
    effective_principal_id,
    effective_workspace_id,
)


class CredentialRuntimeHost(ABC):
    """Dependencies needed to authorize principals and issue credentials."""

    _bootstrapped: Callable[[], bool]
    api_credentials: Repository[ApiCredential]
    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before credential operations."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for credential and audit writes."""
        raise NotImplementedError


class CredentialRuntimeMixin(CredentialRuntimeHost):
    """Implement role checks and lifecycle for service/API credentials."""

    def create_service_account(
        self,
        *,
        name: str,
        permissions: tuple[Permission, ...],
        expires_in_days: int | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[Principal, Role, ApiCredential, str]:
        """Create a service principal, its role, and one-time API token."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(requested_by_principal_id, Permission.USERS_MANAGE)
        self.authorize(requested_by_principal_id, Permission.ROLES_MANAGE)
        if not permissions:
            raise ValueError("service account must have at least one permission")
        if len(permissions) != len(set(permissions)):
            raise ValueError("service account permissions must be unique")
        if expires_in_days is not None and not 1 <= expires_in_days <= 365:
            raise ValueError("expires_in_days must be between 1 and 365")
        timestamp = _now()
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        principal = Principal(
            id=_id(),
            org_id=effective_org_id(),
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            status=PrincipalStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        role = Role(
            id=_id(),
            org_id=effective_org_id(),
            name=name,
            permissions=list(permissions),
        )
        binding = RoleBinding(
            id=_id(),
            org_id=effective_org_id(),
            workspace_id=effective_workspace_id(),
            principal_id=principal.id,
            role_id=role.id,
            created_at=timestamp,
        )
        credential = ApiCredential(
            id=_id(),
            token_digest=digest,
            org_id=effective_org_id(),
            principal_id=principal.id,
            status=ApiCredentialStatus.ACTIVE,
            expires_at=(
                timestamp + timedelta(days=expires_in_days)
                if expires_in_days is not None
                else None
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._persist_service_account(
            principal,
            role,
            binding,
            credential,
            requested_by_principal_id,
            permissions,
            timestamp,
        )
        return principal, role, credential, token

    def _persist_service_account(
        self,
        principal: Principal,
        role: Role,
        binding: RoleBinding,
        credential: ApiCredential,
        actor: str,
        permissions: tuple[Permission, ...],
        timestamp: datetime,
    ) -> None:
        with self._unit_of_work():
            self.principals.create(principal)
            self.roles.create(role)
            self.role_bindings.create(binding)
            self.api_credentials.create(credential)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=actor,
                    action="service_account.created",
                    target_type="principal",
                    target_id=principal.id,
                    summary="Service account and API credential created",
                    request_id=principal.id,
                    metadata={
                        "role_id": role.id,
                        "permissions": [permission.value for permission in permissions],
                    },
                    created_at=timestamp,
                )
            )

    def revoke_api_credential(
        self,
        credential_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> ApiCredential:
        """Revoke a persisted API credential; repeating revocation is safe."""
        if not self._bootstrapped():
            self.bootstrap()
        requested_by_principal_id = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(requested_by_principal_id, Permission.USERS_MANAGE)
        credential = self.api_credentials.get_scoped(credential_id, effective_org_id())
        if credential.status is ApiCredentialStatus.REVOKED:
            return credential
        timestamp = _now()
        revoked = ApiCredential.model_validate(
            credential.model_copy(
                update={
                    "status": ApiCredentialStatus.REVOKED,
                    "revoked_at": timestamp,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.api_credentials.put(revoked)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=effective_org_id(),
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=requested_by_principal_id,
                    action="api_credential.revoked",
                    target_type="api_credential",
                    target_id=credential.id,
                    summary="API credential revoked",
                    request_id=credential.id,
                    metadata={"principal_id": credential.principal_id},
                    created_at=timestamp,
                )
            )
        return revoked

    def rotate_api_credential(
        self,
        credential_id: str,
        *,
        requested_by_principal_id: str = "local-developer",
    ) -> tuple[ApiCredential, str]:
        """Replace an active credential while preserving identity and expiry."""
        if not self._bootstrapped():
            self.bootstrap()
        actor = effective_principal_id(requested_by_principal_id)
        self.authorize_organization(actor, Permission.USERS_MANAGE)
        credential = self.api_credentials.get_scoped(credential_id, effective_org_id())
        if credential.status is not ApiCredentialStatus.ACTIVE:
            raise ValueError("only active API credentials can be rotated")
        timestamp = _now()
        if credential.expires_at is not None and credential.expires_at <= timestamp:
            raise ValueError("expired API credentials cannot be rotated")

        token = secrets.token_urlsafe(32)
        replacement = ApiCredential(
            id=_id(),
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            org_id=credential.org_id,
            principal_id=credential.principal_id,
            status=ApiCredentialStatus.ACTIVE,
            expires_at=credential.expires_at,
            created_at=timestamp,
            updated_at=timestamp,
        )
        revoked = ApiCredential.model_validate(
            credential.model_copy(
                update={
                    "status": ApiCredentialStatus.REVOKED,
                    "revoked_at": timestamp,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            rotated = self.api_credentials.put_if_status_and_stale(
                revoked,
                expected_status=ApiCredentialStatus.ACTIVE.value,
                updated_before=credential.updated_at,
            )
            if not rotated:
                raise ValueError("API credential changed before rotation; retry")
            self.api_credentials.create(replacement)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=credential.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=actor,
                    action="api_credential.rotated",
                    target_type="api_credential",
                    target_id=credential.id,
                    summary="API credential rotated",
                    request_id=replacement.id,
                    metadata={
                        "principal_id": credential.principal_id,
                        "replacement_id": replacement.id,
                    },
                    created_at=timestamp,
                )
            )
        return replacement, token

    def authenticate_principal(self, principal_id: str) -> str:
        """Resolve an API token subject to an active principal."""
        if not self._bootstrapped():
            self.bootstrap()
        principal = self.principals.get(principal_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise PermissionError("principal is not active")
        return principal.id

    def authorize(self, principal_id: str, permission: Permission) -> None:
        """Require an active principal to hold a permission in its workspace."""
        principal_id = effective_principal_id(principal_id)
        org_id = effective_org_id()
        workspace_id = effective_workspace_id()
        principal = self.principals.get_scoped(principal_id, org_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise PermissionError("principal is not active")
        bindings = self.role_bindings.list_scoped(org_id)
        for binding in bindings:
            if binding.principal_id != principal_id:
                continue
            if binding.workspace_id not in {None, workspace_id}:
                continue
            role = self.roles.get_scoped(binding.role_id, org_id)
            if permission in role.permissions:
                return
        raise PermissionError(f"principal lacks {permission.value} permission")

    def authorize_organization(self, principal_id: str, permission: Permission) -> None:
        """Require a permission granted by an organization-wide role binding."""
        principal_id = effective_principal_id(principal_id)
        org_id = effective_org_id()
        principal = self.principals.get_scoped(principal_id, org_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise PermissionError("principal is not active")
        for binding in self.role_bindings.list_scoped(org_id):
            if binding.principal_id != principal_id or binding.workspace_id is not None:
                continue
            role = self.roles.get_scoped(binding.role_id, org_id)
            if permission in role.permissions:
                return
        raise PermissionError(
            f"principal lacks organization-level {permission.value} permission"
        )
