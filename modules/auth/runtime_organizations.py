"""Organization provisioning and principal workspace resolution."""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from application.ports.repository import Repository
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    ApprovalPolicy,
    AuditEvent,
    AuditTargetScope,
    ModelProfile,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
    Organization,
    OrganizationSettings,
    OrganizationStatus,
    Principal,
    PrincipalStatus,
    PrincipalType,
    RiskLevel,
    Role,
    RoleBinding,
    User,
    UserStatus,
    Workspace,
    WorkspaceStatus,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.auth.organization_slugs import OrganizationSlugs
from modules.auth.role_presets import build_role_preset
from shared.clock import new_id as _id
from shared.clock import utc_now as _now
from shared.request_context import reset_request_principal, set_request_identity


class MockModelIdentifier(Protocol):
    """Minimal local model property needed to seed a new organization."""

    model_id: str
    provider_id: str


@dataclass(frozen=True)
class OwnerRecords:
    """Identity and credential records provisioned for an organization owner."""

    organization: Organization
    workspace: Workspace
    user: User
    principal: Principal
    role: Role
    preset_roles: tuple[Role, ...]
    binding: RoleBinding
    credential: ApiCredential | None
    token: str | None


@dataclass(frozen=True)
class OrganizationDefaults:
    """Platform records installed with a newly created organization."""

    provider: ModelProviderContract
    profile: ModelProfile
    policy: ApprovalPolicy


class OrganizationRuntimeHost(ABC):
    """Dependencies required to provision organizations and owner workspaces."""

    _bootstrapped: Callable[[], bool]
    api_credentials: Repository[ApiCredential]
    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    model: MockModelIdentifier
    model_profiles: Repository[ModelProfile]
    organizations: Repository[Organization]
    organization_settings: Repository[OrganizationSettings]
    principals: Repository[Principal]
    providers: Repository[ModelProviderContract]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    slugs: OrganizationSlugs | None
    users: Repository[User]
    workspaces: Repository[Workspace]

    @abstractmethod
    def bootstrap(self) -> None:
        """Initialize product defaults before onboarding."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for atomic organization provisioning."""
        raise NotImplementedError

    @abstractmethod
    def list_principal_workspaces(self, principal_id: str) -> tuple[Workspace, ...]:
        """Resolve workspace access from the workspace membership capability."""
        raise NotImplementedError


class OrganizationRuntimeMixin(OrganizationRuntimeHost):
    """Provision organizations and resolve principal workspace access."""

    def ensure_bootstrapped(self) -> None:
        """Initialize product defaults once, before any provisioning transaction."""
        if not self._bootstrapped():
            self.bootstrap()

    def provision_organization(
        self,
        *,
        name: str,
        email: str,
        display_name: str,
        audit_metadata: dict[str, Any],
        issue_credential: bool = True,
        within: Callable[[OwnerRecords], None] | None = None,
    ) -> OwnerRecords:
        """Provision one organization and its owner as a single unit of work.

        ``within`` runs inside the same unit of work, after the tenant rows and
        under the new organization's own scope, so a caller can add records
        that must commit or roll back together with the tenant. Account-scoped
        callers switch to their system scope inside that callback.

        The URL slug is reserved first, inside the same unit of work: the one in
        ``audit_metadata["slug"]`` when given (``SlugTaken`` if it is held),
        otherwise the first free slug derived from the name.
        """
        self.ensure_bootstrapped()
        normalized_email = email.strip().casefold()
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("a valid owner email is required")
        organization_id, workspace_id, timestamp = _id(), _id(), _now()
        owner = self._build_owner_records(
            organization_id,
            workspace_id,
            name,
            normalized_email,
            display_name,
            timestamp,
            issue_credential=issue_credential,
        )
        defaults = self._build_organization_defaults(
            organization_id, workspace_id, timestamp
        )
        self._persist_new_organization(
            owner, defaults, audit_metadata, timestamp, within
        )
        return owner

    @staticmethod
    def _build_owner_records(
        organization_id: str,
        workspace_id: str,
        name: str,
        email: str,
        display_name: str,
        timestamp: datetime,
        *,
        issue_credential: bool = True,
    ) -> OwnerRecords:
        organization = Organization(
            id=organization_id,
            name=name,
            status=OrganizationStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        workspace = Workspace(
            id=workspace_id,
            org_id=organization_id,
            name="Default workspace",
            status=WorkspaceStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        user = User(
            id=_id(),
            org_id=organization_id,
            email=email,
            display_name=display_name,
            status=UserStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        principal = Principal(
            id=_id(),
            org_id=organization_id,
            principal_type=PrincipalType.USER,
            status=PrincipalStatus.ACTIVE,
            user_id=user.id,
            created_at=timestamp,
            updated_at=timestamp,
        )
        role = build_role_preset(organization_id, "admin")
        preset_roles = (
            build_role_preset(organization_id, "builder"),
            build_role_preset(organization_id, "user"),
        )
        binding = RoleBinding(
            id=_id(),
            org_id=organization_id,
            principal_id=principal.id,
            role_id=role.id,
            workspace_id=None,
            created_at=timestamp,
        )
        credential, token = (
            OrganizationRuntimeMixin._issue_credential(
                organization_id, principal.id, timestamp
            )
            if issue_credential
            else (None, None)
        )
        return OwnerRecords(
            organization,
            workspace,
            user,
            principal,
            role,
            preset_roles,
            binding,
            credential,
            token,
        )

    @staticmethod
    def _issue_credential(
        organization_id: str, principal_id: str, timestamp: datetime
    ) -> tuple[ApiCredential, str]:
        """Create the owner's API credential and the one-time token for it."""
        token = secrets.token_urlsafe(32)
        credential = ApiCredential(
            id=_id(),
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            org_id=organization_id,
            principal_id=principal_id,
            status=ApiCredentialStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return credential, token

    def _build_organization_defaults(
        self, organization_id: str, workspace_id: str, timestamp: datetime
    ) -> OrganizationDefaults:
        provider = ModelProviderContract(
            id=_id(),
            org_id=organization_id,
            provider_type=ModelProviderType.LOCAL,
            display_name="Deterministic mock",
            allowed_models=[self.model.model_id],
            status=ModelProviderStatus.ACTIVE,
        )
        profile = ModelProfile(
            id=_id(),
            org_id=organization_id,
            provider_id=provider.id,
            model=self.model.model_id,
            display_name="Local mock model",
            status=ModelProfileStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        policy = ApprovalPolicy(
            approval_policy_id=_id(),
            org_id=organization_id,
            workspace_id=workspace_id,
            name="Require review for medium and high risk actions",
            required_risk_levels=(RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL),
            allow_self_approval=False,
            enabled=True,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return OrganizationDefaults(provider, profile, policy)

    def _persist_new_organization(
        self,
        owner: OwnerRecords,
        defaults: OrganizationDefaults,
        audit_metadata: dict[str, Any],
        timestamp: datetime,
        within: Callable[[OwnerRecords], None] | None = None,
    ) -> None:
        """Write the new tenant's records under that tenant's own scope."""
        identity = set_request_identity(
            owner.principal.id, owner.organization.id, owner.workspace.id
        )
        try:
            self._write_new_organization(
                owner, defaults, audit_metadata, timestamp, within
            )
        finally:
            reset_request_principal(identity)

    def _write_new_organization(
        self,
        owner: OwnerRecords,
        defaults: OrganizationDefaults,
        audit_metadata: dict[str, Any],
        timestamp: datetime,
        within: Callable[[OwnerRecords], None] | None = None,
    ) -> None:
        """Atomically persist the organization, owner, defaults, and audit event."""
        with self._unit_of_work():
            slug = self._reserve_slug(owner.organization.name, audit_metadata)
            self.organizations.create(owner.organization)
            self.organization_settings.create(
                OrganizationSettings(
                    id=owner.organization.id,
                    org_id=owner.organization.id,
                    owner_principal_id=owner.principal.id,
                    timezone=str(audit_metadata.get("timezone", "Asia/Kolkata")),
                    locale=str(audit_metadata.get("locale", "en")),
                    slug=slug,
                    email_domain=audit_metadata.get("email_domain"),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            self.workspaces.create(owner.workspace)
            self.users.create(owner.user)
            self.principals.create(owner.principal)
            self.roles.create(owner.role)
            for role in owner.preset_roles:
                self.roles.create(role)
            self.role_bindings.create(owner.binding)
            if owner.credential is not None:
                self.api_credentials.create(owner.credential)
            self.providers.create(defaults.provider)
            self.model_profiles.create(defaults.profile)
            self.approval_policies.create(defaults.policy)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=owner.organization.id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=owner.principal.id,
                    action="organization.created",
                    target_type="organization",
                    target_id=owner.organization.id,
                    summary="Organization and owner workspace provisioned",
                    request_id=owner.organization.id,
                    metadata=audit_metadata,
                    created_at=timestamp,
                )
            )
            if within is not None:
                within(owner)

    def _reserve_slug(self, name: str, audit_metadata: dict[str, Any]) -> str | None:
        """Hold the requested or derived slug and record it on the audit event."""
        if self.slugs is None:
            return None
        requested = audit_metadata.get("slug")
        slug = (
            self.slugs.reserve(str(requested))
            if requested
            else self.slugs.reserve_derived(name)
        )
        audit_metadata["slug"] = slug
        return slug

    def resolve_principal_scope(
        self,
        principal_id: str,
        workspace_id: str | None = None,
        *,
        owner_recovery: bool = False,
    ) -> tuple[str, str]:
        """Resolve an active principal to an authorized organization/workspace.

        ``owner_recovery`` is passed only by the middleware for the owner's
        pending-deletion allow-list (after account selection admitted exactly
        that owner and route). It skips the organization-active check and
        accepts a missing workspace, because those routes use organization scope.
        """
        if not self._bootstrapped():
            self.bootstrap()
        principal = self.principals.get(principal_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise PermissionError("principal is not active")
        organization = self.organizations.get(principal.org_id)
        if organization.status is not OrganizationStatus.ACTIVE and not owner_recovery:
            raise PermissionError("organization is not active")
        accessible_workspaces = self.list_principal_workspaces(principal_id)
        if workspace_id is not None:
            workspace = next(
                (item for item in accessible_workspaces if item.id == workspace_id),
                None,
            )
            if workspace is None:
                raise PermissionError("principal cannot access requested workspace")
            return principal.org_id, workspace.id
        if accessible_workspaces:
            return principal.org_id, accessible_workspaces[0].id
        if owner_recovery:
            return principal.org_id, "lifecycle"
        raise PermissionError("principal has no active workspace access")
