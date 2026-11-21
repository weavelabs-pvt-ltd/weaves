"""Account directory: verified identities, sessions and membership linking.

Sign-in itself is Google OpenID Connect (``google_sign_in``); this service owns
the global account rows that every sign-in resolves to.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, suppress

from application.ports.repository import Repository
from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    AccountOAuthState,
    AccountSession,
    AccountStatus,
    AuditEvent,
    Organization,
    OrganizationMembership,
    Principal,
    User,
    Workspace,
)
from modules.auth.account_cleanup import PURGE_BATCH, AccountCleanup
from modules.auth.account_linking import MembershipLinker
from modules.auth.account_selection import AccountOrganizationSelector
from modules.auth.account_sessions import AccountSessions
from shared.clock import new_id, utc_now


class AccountSignInService:
    """Own accounts, their identities, sessions and organization memberships."""

    def __init__(
        self,
        *,
        accounts: Repository[Account],
        identities: Repository[AccountIdentity],
        oauth_states: Repository[AccountOAuthState],
        sessions: Repository[AccountSession],
        memberships: Repository[OrganizationMembership],
        users: Repository[User],
        principals: Repository[Principal],
        organizations: Repository[Organization],
        workspaces: Repository[Workspace],
        audit_events: Repository[AuditEvent],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind global account repositories and the tenant directory."""
        self.accounts = accounts
        self.identities = identities
        self.oauth_states = oauth_states
        self.memberships = memberships
        self.organizations = organizations
        self.unit_of_work = unit_of_work
        self.sessions = AccountSessions(sessions, accounts)
        self.selector = AccountOrganizationSelector(
            memberships=memberships,
            users=users,
            principals=principals,
            organizations=organizations,
        )
        self.linker = MembershipLinker(
            users=users,
            principals=principals,
            organizations=organizations,
            memberships=memberships,
            workspaces=workspaces,
            audit_events=audit_events,
            unit_of_work=unit_of_work,
        )
        self._cleanup = AccountCleanup(oauth_states, sessions)

    def purge_expired(self, limit: int = PURGE_BATCH) -> int:
        """Delete at most ``limit`` finished sign-in states and sessions."""
        return self._cleanup.purge(limit)

    def verified_email_identity(self, email: str) -> AccountIdentity | None:
        """Find the one verified email identity, ignoring unverified claims."""
        candidate = self.identities.find_by_field("subject", email)
        if candidate is not None and self.is_verified_email(candidate, email):
            return candidate
        if candidate is None:
            return None
        return next(
            (
                i
                for i in self.identities.list_by_field("subject", email)
                if self.is_verified_email(i, email)
            ),
            None,
        )

    @staticmethod
    def is_verified_email(identity: AccountIdentity, email: str) -> bool:
        """Only a verified email identity for this exact address can sign in."""
        return (
            identity.kind is AccountIdentityKind.EMAIL
            and identity.subject == email
            and identity.verified_at is not None
        )

    def find_account(self, email: str) -> Account | None:
        """Return the account that owns a verified email, never creating one."""
        identity = self.verified_email_identity(email.strip().casefold())
        return self.accounts.get(identity.account_id) if identity else None

    def find_or_create_account(self, email: str) -> Account:
        """Resolve the platform-wide owner of a verified email, creating it once."""
        identity = self.verified_email_identity(email)
        if identity is None:
            now = utc_now()
            account = Account(
                id=new_id(),
                display_name=email.partition("@")[0][:160] or email[:160],
                status=AccountStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            created = AccountIdentity(
                id=new_id(),
                account_id=account.id,
                kind=AccountIdentityKind.EMAIL,
                subject=email,
                verified_at=now,
                created_at=now,
                updated_at=now,
            )
            try:
                with self.unit_of_work():
                    self.accounts.create(account)
                    self.identities.create(created)
                return account
            except ValueError:  # lost the race on the verified-identity index
                self._discard(account)
                identity = self.verified_email_identity(email)
                if identity is None:
                    raise
        return self.accounts.get(identity.account_id)

    def _discard(self, account: Account) -> None:
        """Remove an orphan account when its identity insert was rejected."""
        with suppress(KeyError):
            self.accounts.delete(account.id)
