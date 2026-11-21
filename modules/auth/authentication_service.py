"""API-token authentication and principal identity lookups."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from application.ports.repository import Repository
from contracts.v1 import ApiCredential, ApiCredentialStatus, Principal, User
from shared.clock import utc_now


class AuthenticationService:
    """Resolve service credentials and principals through injected adapters."""

    def __init__(
        self,
        *,
        api_credentials: Repository[ApiCredential],
        principals: Repository[Principal],
        users: Repository[User],
        is_bootstrapped: Callable[[], bool],
        bootstrap: Callable[[], None],
        authenticate_principal: Callable[[str], str],
    ) -> None:
        """Bind credential and identity repositories and lifecycle ports."""
        self.api_credentials = api_credentials
        self.principals = principals
        self.users = users
        self._bootstrapped = is_bootstrapped
        self._bootstrap = bootstrap
        self._authenticate_principal = authenticate_principal

    def bootstrap(self) -> None:
        """Delegate product initialization to the application lifecycle."""
        self._bootstrap()

    def authenticate_principal(self, principal_id: str) -> str:
        """Resolve a principal through credential and membership policy."""
        return self._authenticate_principal(principal_id)

    def authenticate_api_token(self, token: str) -> str | None:
        """Resolve a persisted high-entropy API token to its active principal."""
        if not self._bootstrapped():
            self.bootstrap()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        credential = self.api_credentials.find_by_field("token_digest", digest)
        if credential is None or credential.status is not ApiCredentialStatus.ACTIVE:
            return None
        if credential.expires_at is not None and credential.expires_at <= utc_now():
            return None
        try:
            return self.authenticate_principal(credential.principal_id)
        except (KeyError, PermissionError):
            return None

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
