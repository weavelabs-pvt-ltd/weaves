"""Shared helpers for account-created organization tests (memory and PostgreSQL)."""

from __future__ import annotations

from dataclasses import dataclass

from infrastructure.auth.throttle import InMemoryLoginRateLimiter
from tests.support.account_session import sign_in

URL = "/api/v0/account/organizations"
ENABLE = "WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION"
# One create call per row a successful provisioning writes, in order:
# organization, workspace, user, principal, 3 roles, binding, provider, profile,
# policy, audit event, membership, idempotency record.
PROVISIONING_WRITES = 14
PROVISIONED_COLLECTIONS = (
    "organizations",
    "workspaces",
    "users",
    "principals",
    "roles",
    "role_bindings",
    "api_credentials",
    "model_providers",
    "model_profiles",
    "approval_policies",
    "audit_events",
    "memberships",
    "account_organization_requests",
)


@dataclass
class Signer:
    """Issue account bearer sessions directly for organization tests."""

    runtime: object

    def sign_in(self, email: str) -> tuple[object, str]:
        """Return (account, bearer token) for a verified email."""
        return sign_in(self.runtime.account_sign_in_service, email)


def prepare(runtime) -> Signer:
    """Give organization creation a roomy throttle and return a signer."""
    runtime.account_organization_service.login_rate_limiter = InMemoryLoginRateLimiter(
        limit=500
    )
    return Signer(runtime)


def bearer(token: str, key: str | None = None, org: str | None = None) -> dict:
    """Headers for an account bearer call."""
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    if org is not None:
        headers["X-Organization-Id"] = org
    return headers


def create_repositories(service) -> dict:
    """The repositories one provisioning writes, keyed by collection."""
    provisioner = service._provisioner
    return {
        "organizations": provisioner.organizations,
        "workspaces": provisioner.workspaces,
        "users": provisioner.users,
        "principals": provisioner.principals,
        "roles": provisioner.roles,
        "role_bindings": provisioner.role_bindings,
        "api_credentials": provisioner.api_credentials,
        "model_providers": provisioner.providers,
        "model_profiles": provisioner.model_profiles,
        "approval_policies": provisioner.approval_policies,
        "audit_events": provisioner.audit_events,
        "memberships": service._memberships,
        "account_organization_requests": service._requests,
    }


def fail_on_write(monkeypatch, service, number: int) -> dict:
    """Make the Nth repository create across all provisioning writes raise."""
    state = {"calls": 0}
    for repository in create_repositories(service).values():
        original = repository.create

        def counted(record, _original=original):
            state["calls"] += 1
            if state["calls"] == number:
                raise RuntimeError(f"injected failure at write {number}")
            return _original(record)

        monkeypatch.setattr(repository, "create", counted)
    return state
