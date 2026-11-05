"""Organization onboarding and principal scope resolution use cases."""

from __future__ import annotations


from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    ApiCredential,
    ApprovalPolicy,
    AuditEvent,
    KnowledgeDocument,
    ModelProfile,
    Organization,
    PluginInstallation,
    Principal,
    Role,
    RoleBinding,
    User,
    Workspace,
)
from contracts.v1 import ModelProvider as ModelProviderContract
from modules.auth.runtime_organizations import (
    MockModelIdentifier,
    OrganizationRuntimeMixin,
)
from shared.request_context import effective_org_id


class OrganizationService(OrganizationRuntimeMixin):
    """Provision organizations and resolve workspace scope through explicit ports."""

    api_credentials: Repository[ApiCredential]
    approval_policies: Repository[ApprovalPolicy]
    audit_events: Repository[AuditEvent]
    installations: Repository[PluginInstallation]
    knowledge_documents: Repository[KnowledgeDocument]
    model_profiles: Repository[ModelProfile]
    organizations: Repository[Organization]
    principals: Repository[Principal]
    providers: Repository[ModelProviderContract]
    role_bindings: Repository[RoleBinding]
    roles: Repository[Role]
    users: Repository[User]
    workspaces: Repository[Workspace]

    def __init__(
        self,
        *,
        api_credentials: Repository[ApiCredential],
        approval_policies: Repository[ApprovalPolicy],
        audit_events: Repository[AuditEvent],
        installations: Repository[PluginInstallation],
        knowledge_documents: Repository[KnowledgeDocument],
        model_profiles: Repository[ModelProfile],
        organizations: Repository[Organization],
        principals: Repository[Principal],
        providers: Repository[ModelProviderContract],
        role_bindings: Repository[RoleBinding],
        roles: Repository[Role],
        users: Repository[User],
        workspaces: Repository[Workspace],
        model: MockModelIdentifier,
        onboarding_rate_limiter: LoginRateLimiter,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        list_principal_workspaces: Callable[[str], tuple[Workspace, ...]],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind organization repositories and onboarding application ports."""
        self.api_credentials = api_credentials
        self.approval_policies = approval_policies
        self.audit_events = audit_events
        self.installations = installations
        self.knowledge_documents = knowledge_documents
        self.model_profiles = model_profiles
        self.organizations = organizations
        self.principals = principals
        self.providers = providers
        self.role_bindings = role_bindings
        self.roles = roles
        self.users = users
        self.workspaces = workspaces
        self.model = model
        self.onboarding_rate_limiter = onboarding_rate_limiter
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._list_principal_workspaces = list_principal_workspaces
        self._open_unit_of_work = unit_of_work

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for organization and owner provisioning."""
        return self._open_unit_of_work()

    def get_current_organization(self) -> Organization:
        """Load the organization selected by the active request context."""
        if not self._bootstrapped():
            self.bootstrap()
        return self.organizations.get(effective_org_id())

    def list_principal_workspaces(self, principal_id: str) -> tuple[Workspace, ...]:
        """Delegate workspace access resolution to the membership service."""
        return self._list_principal_workspaces(principal_id)
