"""Let a verified account create an organization atomically and idempotently.

Everything a new tenant needs, the account's membership in it and the
idempotency record commit in one unit of work or not at all. The unit of work
spans two storage scopes without widening either: tenant rows (organization,
workspace, owner, roles, defaults, membership, audit) are written under the new
organization's own request identity, and the account-scoped idempotency record
and account reads run under the narrow ``account`` system scope. Under
PostgreSQL the store re-applies the role and scope settings on every repository
call inside the transaction, so switching scope never leaves the transaction.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    AccountOrganizationRequest,
    AccountStatus,
    MembershipStatus,
    Organization,
    OrganizationMembership,
    Workspace,
)
from modules.auth.account_organization_input import AccountOrganizationCommand
from modules.auth.organization_service import OrganizationService
from modules.auth.runtime_organizations import OwnerRecords
from shared.clock import new_id, utc_now
from shared.errors import LoginThrottled
from shared.login_buckets import login_bucket_key
from shared.request_context import system_scope

DEFAULT_ORGANIZATION_LIMIT = 3
LIMIT_ENV = "WEAVES_ACCOUNT_ORGANIZATION_LIMIT"
ENABLED_ENV = "WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION"


class IdempotencyConflict(Exception):
    """The key was already used with a different request body."""


class OrganizationLimitReached(Exception):
    """The account has created as many organizations as it may."""


class _LostRace(Exception):
    """Another request claimed this idempotency key first."""


@dataclass(frozen=True)
class AccountOrganizationResult:
    """The organization an account created, and whether this call created it."""

    organization: Organization
    workspace: Workspace
    membership: OrganizationMembership
    created: bool


def creation_enabled() -> bool:
    """Account organization creation is off unless explicitly enabled."""
    value = os.environ.get(ENABLED_ENV, "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def organization_limit() -> int:
    """Per-account organization quota; a malformed value keeps the default."""
    try:
        return max(0, int(os.environ.get(LIMIT_ENV, DEFAULT_ORGANIZATION_LIMIT)))
    except ValueError:
        return DEFAULT_ORGANIZATION_LIMIT


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


class AccountOrganizationService:
    """Provision an organization for an authenticated, verified account."""

    def __init__(
        self,
        *,
        organization_service: OrganizationService,
        identities: Repository[AccountIdentity],
        memberships: Repository[OrganizationMembership],
        requests: Repository[AccountOrganizationRequest],
        login_rate_limiter: LoginRateLimiter,
        unit_of_work: Callable[[], AbstractContextManager[None]],
        advisory_transaction_lock: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the tenant provisioner, account repositories and transaction."""
        self._provisioner = organization_service
        self._identities = identities
        self._memberships = memberships
        self._requests = requests
        self.login_rate_limiter = login_rate_limiter
        self._unit_of_work = unit_of_work
        self._advisory_lock = advisory_transaction_lock

    def create(
        self,
        account: Account,
        command: AccountOrganizationCommand,
        idempotency_key: str,
        client_address: str | None,
    ) -> AccountOrganizationResult:
        """Create the organization once per account and key; replay otherwise."""
        if account.status is not AccountStatus.ACTIVE:
            raise PermissionError("account is not active")
        if not 1 <= len(idempotency_key) <= 128:
            raise ValueError("Idempotency-Key must be 1 to 128 characters")
        self._throttle(account, client_address)
        self._provisioner.ensure_bootstrapped()
        key_digest = _digest("weaves-account-org-key-v1", idempotency_key)
        request_id = _digest(account.id, key_digest)
        fingerprint = command.fingerprint()
        try:
            return self._create_once(
                account, command, key_digest, request_id, fingerprint
            )
        except _LostRace:
            with system_scope("account"):
                existing = self._requests.get(request_id)
            return self._replay(account, existing, fingerprint)

    def _throttle(self, account: Account, client_address: str | None) -> None:
        """Budget attempts per account and per source address."""
        buckets = [login_bucket_key("account-organization", account.id)]
        address = (client_address or "").strip()
        if address:
            buckets.append(login_bucket_key("account-organization-source", address))
        now = utc_now()
        retry_after = max(
            (
                decision.retry_after_seconds
                for decision in (
                    self.login_rate_limiter.consume(bucket, now) for bucket in buckets
                )
                if not decision.allowed
            ),
            default=0,
        )
        if retry_after:
            raise LoginThrottled(retry_after)

    def _create_once(
        self,
        account: Account,
        command: AccountOrganizationCommand,
        key_digest: str,
        request_id: str,
        fingerprint: str,
    ) -> AccountOrganizationResult:
        """Check, provision and record in one unit of work."""
        created: list[OrganizationMembership] = []
        with self._unit_of_work():
            if self._advisory_lock is not None:
                self._advisory_lock(f"account-organization:{account.id}")
            with system_scope("account"):
                existing = self._find(request_id)
                if existing is not None:
                    return self._replay(account, existing, fingerprint)
                self._enforce_quota(account)
                email = self._owner_email(account, command.owner_email)

            def record(owner: OwnerRecords) -> None:
                created.append(
                    self._link(account, owner, key_digest, request_id, fingerprint)
                )

            owner = self._provisioner.provision_organization(
                name=command.name,
                email=email,
                display_name=command.display_name or account.display_name,
                audit_metadata={
                    "created_via": "account",
                    "account_id": account.id,
                    "timezone": command.timezone,
                    "locale": command.locale,
                },
                issue_credential=False,
                within=record,
            )
        return AccountOrganizationResult(
            owner.organization, owner.workspace, created[0], True
        )

    def _link(
        self,
        account: Account,
        owner: OwnerRecords,
        key_digest: str,
        request_id: str,
        fingerprint: str,
    ) -> OrganizationMembership:
        """Write the membership (tenant scope) and idempotency record (account)."""
        now = utc_now()
        membership = OrganizationMembership(
            id=new_id(),
            org_id=owner.organization.id,
            account_id=account.id,
            user_id=owner.user.id,
            principal_id=owner.principal.id,
            status=MembershipStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        self._memberships.create(membership)
        with system_scope("account"):
            try:
                self._requests.create(
                    AccountOrganizationRequest(
                        id=request_id,
                        account_id=account.id,
                        key_digest=key_digest,
                        request_fingerprint=fingerprint,
                        organization_id=owner.organization.id,
                        created_at=now,
                    )
                )
            except ValueError as exc:  # unique key: another request won
                raise _LostRace from exc
        return membership

    def _find(self, request_id: str) -> AccountOrganizationRequest | None:
        try:
            return self._requests.get(request_id)
        except KeyError:
            return None

    def _enforce_quota(self, account: Account) -> None:
        used = len(self._requests.list_by_field("account_id", account.id))
        if used >= organization_limit():
            raise OrganizationLimitReached

    def _owner_email(self, account: Account, requested: str | None) -> str:
        """Pick a verified email of this account; default to the earliest."""
        verified = sorted(
            (
                identity
                for identity in self._identities.list_by_field("account_id", account.id)
                if identity.kind is AccountIdentityKind.EMAIL
                and identity.verified_at is not None
            ),
            key=lambda identity: (identity.verified_at, identity.created_at),
        )
        if requested is None:
            if not verified:
                raise ValueError("the account has no verified email")
            return verified[0].subject
        if requested not in {identity.subject for identity in verified}:
            raise ValueError("owner_email must be a verified email on your account")
        return requested

    def _replay(
        self,
        account: Account,
        existing: AccountOrganizationRequest,
        fingerprint: str,
    ) -> AccountOrganizationResult:
        """Return the organization an earlier identical request created."""
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict
        with system_scope("account"):
            organization = self._provisioner.organizations.get(existing.organization_id)
            workspace = next(
                iter(self._provisioner.workspaces.list_scoped(organization.id))
            )
            membership = next(
                item
                for item in self._memberships.list_by_field("account_id", account.id)
                if item.org_id == organization.id
            )
        return AccountOrganizationResult(organization, workspace, membership, False)
