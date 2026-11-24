"""Google sign-in: single-use state records, PKCE, and the callback outcome."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from contracts.v1 import Account, AccountOAuthState, AccountSession
from modules.auth.account_sign_in import AccountSignInService
from modules.auth.google_account_resolution import GoogleAccountResolver, SignInRefused
from modules.auth.google_oidc import (
    GoogleConfig,
    GoogleOidcProvider,
    ProviderRejected,
    ProviderUnavailable,
)
from shared.clock import new_id, utc_now

logger = logging.getLogger("product.account")

STATE_MINUTES = 10
MAX_RETURN_TO = 512
DEFAULT_RETURN_TO = "/"


def safe_return_to(value: str | None) -> str | None:
    """Return a same-origin relative path, or None when the value is unsafe."""
    if value is None or value == "":
        return DEFAULT_RETURN_TO
    if (
        len(value) > MAX_RETURN_TO
        or not value.startswith("/")
        or value.startswith("//")
        or any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in value)
    ):
        return None
    return value


def digest(value: str) -> str:
    """Return the only form of a state or binding secret that is stored."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _challenge(verifier: str) -> str:
    """Derive the PKCE S256 code challenge."""
    raw = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class GoogleStart:
    """Where to send the browser and the secret that binds it to this attempt."""

    authorization_url: str
    binding: str


@dataclass(frozen=True)
class GoogleSignInResult:
    """A signed-in account with its new browser session."""

    account: Account
    session: AccountSession
    token: str
    return_to: str


class GoogleSignIn:
    """Run the authorization-code flow for the single Google sign-in method."""

    def __init__(
        self,
        config: GoogleConfig,
        provider: GoogleOidcProvider,
        service: AccountSignInService,
        *,
        signups_open: Callable[[], bool] | None = None,
    ) -> None:
        """Bind settings, the provider, the account directory and the sign-up switch."""
        self.config = config
        self.provider = provider
        self.service = service
        self._resolver = GoogleAccountResolver(service, signups_open=signups_open)

    def start(self, return_to: str) -> GoogleStart:
        """Create one 10-minute, single-use attempt bound to a fresh cookie secret."""
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        url = self.provider.authorization_url(state, nonce, _challenge(verifier))
        now = utc_now()
        self.service.oauth_states.create(
            AccountOAuthState(
                id=new_id(),
                state_digest=digest(state),
                binding_digest=digest(binding),
                nonce=nonce,
                code_verifier=verifier,
                return_to=return_to,
                expires_at=now + timedelta(minutes=STATE_MINUTES),
                created_at=now,
                updated_at=now,
            )
        )
        return GoogleStart(url, binding)

    def _consume(self, state: str | None, binding: str | None) -> AccountOAuthState:
        """Consume the attempt once; it must be live and bound to this browser."""
        if not state or len(state) > 256:
            raise SignInRefused("state")
        record = self.service.oauth_states.find_by_field("state_digest", digest(state))
        if record is None:
            raise SignInRefused("state")
        try:
            self.service.oauth_states.consume(record.id)
        except KeyError as exc:
            raise SignInRefused("state") from exc
        if record.expires_at <= utc_now() or not binding:
            raise SignInRefused("state")
        if not hmac.compare_digest(record.binding_digest, digest(binding)):
            raise SignInRefused("state")
        return record

    def finish(
        self,
        *,
        code: str | None,
        state: str | None,
        binding: str | None,
        provider_error: str | None,
    ) -> GoogleSignInResult:
        """Complete a callback, or raise ``SignInRefused`` with a fixed code."""
        record = self._consume(state, binding)
        if provider_error:
            raise SignInRefused(
                "access_denied" if provider_error == "access_denied" else "unavailable"
            )
        if not code or len(code) > 2048:
            raise SignInRefused("state")
        try:
            claims = self.provider.redeem(code, record.code_verifier, record.nonce)
        except ProviderRejected as exc:
            raise SignInRefused("access_denied") from exc
        except ProviderUnavailable as exc:
            raise SignInRefused("unavailable") from exc
        if not self.config.allows(claims.email):
            raise SignInRefused("not_allowed")
        account = self._resolver.resolve(claims)
        self.service.linker.link(account, claims.email)
        session, token = self.service.sessions.issue(account, "browser")
        logger.info("account google sign-in ok account_id=%s", account.id)
        return GoogleSignInResult(account, session, token, record.return_to)
