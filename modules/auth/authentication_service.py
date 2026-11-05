"""Password, API-token, and user-session authentication use cases."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from application.ports.email import EmailSender
from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    ApiCredential,
    AuditEvent,
    EmailLoginChallenge,
    Principal,
    User,
    UserPasswordCredential,
    UserSession,
)
from modules.auth.runtime_sessions import AuthenticationRuntimeMixin


class AuthenticationService(AuthenticationRuntimeMixin):
    """Authenticate principals and manage sessions through injected adapters."""

    api_credentials: Repository[ApiCredential]
    audit_events: Repository[AuditEvent]
    principals: Repository[Principal]
    user_passwords: Repository[UserPasswordCredential]
    user_sessions: Repository[UserSession]
    users: Repository[User]
    login_rate_limiter: LoginRateLimiter

    def __init__(
        self,
        *,
        api_credentials: Repository[ApiCredential],
        audit_events: Repository[AuditEvent],
        email_login_challenges: Repository[EmailLoginChallenge],
        email_sender: EmailSender,
        email_login_hmac_key: str,
        principals: Repository[Principal],
        user_passwords: Repository[UserPasswordCredential],
        user_sessions: Repository[UserSession],
        users: Repository[User],
        login_rate_limiter: LoginRateLimiter,
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authenticate_principal: Callable[[str], str],
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind authentication repositories, throttling, and lifecycle ports."""
        self.api_credentials = api_credentials
        self.audit_events = audit_events
        self.email_login_challenges = email_login_challenges
        self.email_sender = email_sender
        self.email_login_hmac_key = email_login_hmac_key
        self.principals = principals
        self.user_passwords = user_passwords
        self.user_sessions = user_sessions
        self.users = users
        self.login_rate_limiter = login_rate_limiter
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authenticate_principal = authenticate_principal
        self._open_unit_of_work = unit_of_work

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authenticate_principal(self, principal_id: str) -> str:
        """Resolve a principal through credential and membership policy."""
        return self._authenticate_principal(principal_id)

    def get_authenticated_identity(
        self, principal_id: str
    ) -> tuple[Principal, User | None]:
        """Load a principal and its optional human user record."""
        if not self._bootstrapped():
            self.bootstrap()
        principal = self.principals.get(principal_id)
        user = (
            self.users.get_scoped(principal.user_id, principal.org_id)
            if principal.user_id is not None
            else None
        )
        return principal, user

    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for related authentication state changes."""
        return self._open_unit_of_work()
