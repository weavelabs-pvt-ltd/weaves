"""Organization onboarding and principal workspace resolution."""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    ApprovalPolicy,
    AuditEvent,
    AuditTargetScope,
    KnowledgeDocument,
    ModelProfile,
    ModelProfileStatus,
    ModelProviderStatus,
    ModelProviderType,
    Organization,
    OrganizationStatus,
    PluginInstallation,
    PluginInstallationStatus,
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
from modules.auth.role_presets import build_role_preset
from shared.clock import new_id as _id
from shared.clock import utc_now as _auth_now
from shared.clock import utc_now as _now
from shared.errors import RateLimitExceeded
from shared.login_buckets import login_bucket_key


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
    credential: ApiCredential
    token: str


@dataclass(frozen=True)
class OrganizationDefaults:
    """Platform records installed with a newly created organization."""

    provider: ModelProviderContract
    profile: ModelProfile
    policy: ApprovalPolicy
    installation: PluginInstallation
    document: KnowledgeDocument


class OrganizationRuntimeHost(ABC):
    """Dependencies required to provision organizations and owner workspaces."""

    _bootstrapped: Callable[[], bool]
    api_credentials: Repository[ApiCredential]
    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    knowledge_documents: Repository[KnowledgeDocument]
    model: MockModelIdentifier
    model_profiles: Repository[ModelProfile]
    onboarding_rate_limiter: LoginRateLimiter
    organizations: Repository[Organization]
    principals: Repository[Principal]
    providers: Repository[ModelProviderContract]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
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

    def create_organization(
        self, *, name: str, email: str, display_name: str
    ) -> tuple[Organization, Workspace, User, Principal, str]:
        """Provision an organization, owner workspace, user, and API token."""
        if not self._bootstrapped():
            self.bootstrap()
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
        )
        defaults = self._build_organization_defaults(
            organization_id, workspace_id, timestamp
        )
        self._persist_new_organization(owner, defaults, normalized_email, timestamp)
        return (
            owner.organization,
            owner.workspace,
            owner.user,
            owner.principal,
            owner.token,
        )

    @staticmethod
    def _build_owner_records(
        organization_id: str,
        workspace_id: str,
        name: str,
        email: str,
        display_name: str,
        timestamp: datetime,
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
        token = secrets.token_urlsafe(32)
        credential = ApiCredential(
            id=_id(),
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            org_id=organization_id,
            principal_id=principal.id,
            status=ApiCredentialStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
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
        installation = PluginInstallation(
            plugin_installation_id=_id(),
            org_id=organization_id,
            workspace_id=workspace_id,
            plugin_id="local-knowledge",
            display_name="Local knowledge",
            enabled_capability_ids=("knowledge.search",),
            status=PluginInstallationStatus.ACTIVE,
            created_at=timestamp,
            updated_at=timestamp,
        )
        document = KnowledgeDocument(
            document_id=_id(),
            source_id=f"onboarding:{organization_id}",
            org_id=organization_id,
            workspace_id=workspace_id,
            title="Getting started",
            text=(
                "Add organization knowledge and connect tools to give agents useful "
                "context."
            ),
            is_seeded=True,
            created_at=timestamp,
            updated_at=timestamp,
        )
        return OrganizationDefaults(provider, profile, policy, installation, document)

    def _persist_new_organization(
        self,
        owner: OwnerRecords,
        defaults: OrganizationDefaults,
        normalized_email: str,
        timestamp: datetime,
    ) -> None:
        """Atomically persist the organization, owner, defaults, and audit event."""
        with self._unit_of_work():
            self.organizations.create(owner.organization)
            self.workspaces.create(owner.workspace)
            self.users.create(owner.user)
            self.principals.create(owner.principal)
            self.roles.create(owner.role)
            for role in owner.preset_roles:
                self.roles.create(role)
            self.role_bindings.create(owner.binding)
            self.api_credentials.create(owner.credential)
            self.providers.create(defaults.provider)
            self.model_profiles.create(defaults.profile)
            self.approval_policies.create(defaults.policy)
            self.installations.create(defaults.installation)
            self.knowledge_documents.create(defaults.document)
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
                    metadata={"owner_email": normalized_email},
                    created_at=timestamp,
                )
            )

    def check_onboarding_rate_limit(self, client_address: str | None) -> None:
        """Limit public organization provisioning per client address."""
        normalized_address = (client_address or "").strip()
        if not normalized_address:
            raise RateLimitExceeded(3600)
        bucket = login_bucket_key("onboarding-source", normalized_address)
        decision = self.onboarding_rate_limiter.consume(bucket, _auth_now())
        if not decision.allowed:
            raise RateLimitExceeded(decision.retry_after_seconds)

    def resolve_principal_scope(
        self, principal_id: str, workspace_id: str | None = None
    ) -> tuple[str, str]:
        """Resolve an active principal to an authorized organization/workspace."""
        if not self._bootstrapped():
            self.bootstrap()
        principal = self.principals.get(principal_id)
        if principal.status is not PrincipalStatus.ACTIVE:
            raise PermissionError("principal is not active")
        organization = self.organizations.get(principal.org_id)
        if organization.status is not OrganizationStatus.ACTIVE:
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
        raise PermissionError("principal has no active workspace access")
