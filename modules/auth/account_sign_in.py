"""Account-level email-code sign-in that issues global account sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from datetime import timedelta

from application.ports.email import EmailSender
from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    Account,
    AccountIdentity,
    AccountIdentityKind,
    AccountLoginChallenge,
    AccountSession,
    AccountStatus,
    Organization,
    OrganizationMembership,
    Principal,
    User,
)
from modules.auth.account_linking import MembershipLinker
from modules.auth.account_selection import AccountOrganizationSelector
from modules.auth.account_sessions import AccountSessions, Channel
from shared.clock import new_id, utc_now
from shared.errors import EmailDeliveryUnavailable, LoginThrottled
from shared.login_buckets import login_bucket_key

CODE_MINUTES = 10
MAX_ATTEMPTS = 5
_INVALID = "email code is invalid or expired"


class AccountSignInService:
    """Prove control of an email address and open an account session."""

    def __init__(
        self,
        *,
        accounts: Repository[Account],
        identities: Repository[AccountIdentity],
        challenges: Repository[AccountLoginChallenge],
        sessions: Repository[AccountSession],
        memberships: Repository[OrganizationMembership],
        users: Repository[User],
        principals: Repository[Principal],
        organizations: Repository[Organization],
        email_sender: EmailSender,
        email_login_hmac_key: str,
        login_rate_limiter: LoginRateLimiter,
        unit_of_work: Callable[[], AbstractContextManager[None]],
    ) -> None:
        """Bind global account repositories, the tenant directory and ports."""
        self.accounts = accounts
        self.identities = identities
        self.challenges = challenges
        self.memberships = memberships
        self.organizations = organizations
        self.email_sender = email_sender
        self.email_login_hmac_key = email_login_hmac_key
        self.login_rate_limiter = login_rate_limiter
        self._unit_of_work = unit_of_work
        self.sessions = AccountSessions(sessions, accounts)
        self.selector = AccountOrganizationSelector(
            memberships=memberships,
            users=users,
            principals=principals,
            organizations=organizations,
        )
        self._linker = MembershipLinker(
            users=users,
            principals=principals,
            organizations=organizations,
            memberships=memberships,
        )

    def start_email_login(self, email: str, client_address: str | None = None) -> str:
        """Send a code to any address and return an opaque challenge id.

        The outcome never depends on whether an account or membership exists.
        """
        if (
            len(self.email_login_hmac_key.encode("utf-8")) < 32
            or not self.email_sender.configured
        ):
            raise EmailDeliveryUnavailable("Email sign-in is not configured")
        normalized = email.strip().casefold()
        self._consume_limits(normalized, client_address, include_source=True)
        if "@" not in normalized or not 3 <= len(normalized) <= 320:
            return new_id()  # unpersisted handle, same shape as a real challenge
        challenge, code = self._new_challenge(normalized)
        self._replace_challenges(challenge)
        try:
            self.email_sender.send(
                normalized,
                "Your Weaves sign-in code",
                f"Your Weaves sign-in code is {code}. It expires in "
                f"{CODE_MINUTES} minutes. "
                "If you did not request it, you can ignore this email.",
            )
        except Exception:
            self.challenges.delete(challenge.challenge_id)
            raise
        return challenge.challenge_id

    def verify_email_login(
        self,
        challenge_id: str,
        code: str,
        client_address: str | None = None,
        channel: Channel = "bearer",
    ) -> tuple[Account, AccountSession, str]:
        """Consume a valid code, link memberships and start an account session."""
        del client_address  # the source budget is spent when codes are requested
        challenge = self.redeem(challenge_id, code, account_id=None)
        account = self._find_or_create_account(challenge.email)
        if account.status is not AccountStatus.ACTIVE:
            raise PermissionError(_INVALID)
        self._linker.link(account, challenge.email)
        session, token = self.sessions.issue(account, channel)
        self.login_rate_limiter.clear(self._email_bucket(challenge.email))
        return account, session, token

    def redeem(
        self, challenge_id: str, code: str, *, account_id: str | None
    ) -> AccountLoginChallenge:
        """Consume one valid code exactly once for sign-in or for one account.

        Sign-in redeems only challenges that name no account, and an
        add-email proof only redeems a challenge naming the calling account,
        so neither can be used for the other.
        """
        try:
            challenge = self.challenges.get(challenge_id)
        except KeyError as exc:
            raise PermissionError(_INVALID) from exc
        if challenge.account_id != account_id:
            raise PermissionError(_INVALID)
        self._consume_limits(challenge.email, None, include_source=False)
        if challenge.expires_at <= utc_now() or challenge.attempts >= MAX_ATTEMPTS:
            self.challenges.delete(challenge_id)
            raise PermissionError(_INVALID)
        if not hmac.compare_digest(
            challenge.code_digest, self._digest(challenge_id, code)
        ):
            self._record_failure(challenge)
            raise PermissionError(_INVALID)
        try:
            self.challenges.consume(challenge_id)
        except KeyError as exc:
            raise PermissionError(_INVALID) from exc
        return challenge

    @staticmethod
    def _email_bucket(email: str) -> str:
        """Global per-address bucket, deliberately not tenant-scoped."""
        return login_bucket_key("account-email-code", email)

    def _consume_limits(
        self, email: str, client_address: str | None, *, include_source: bool
    ) -> None:
        """Throttle per address and, when asked, globally per source address."""
        buckets = [self._email_bucket(email)]
        if include_source and client_address:
            buckets.append(login_bucket_key("account-email-source", client_address))
        retry_after = max(
            (
                decision.retry_after_seconds
                for decision in (
                    self.login_rate_limiter.consume(bucket, utc_now())
                    for bucket in buckets
                )
                if not decision.allowed
            ),
            default=0,
        )
        if retry_after:
            raise LoginThrottled(retry_after)

    def _digest(self, challenge_id: str, code: str) -> str:
        """Bind a short code to its challenge with the server-held HMAC key."""
        return hmac.new(
            self.email_login_hmac_key.encode("utf-8"),
            f"{challenge_id}:{code}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _new_challenge(self, email: str) -> tuple[AccountLoginChallenge, str]:
        """Create an expiring challenge whose code is stored only as an HMAC."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge_id = new_id()
        now = utc_now()
        return (
            AccountLoginChallenge(
                challenge_id=challenge_id,
                email=email,
                code_digest=self._digest(challenge_id, code),
                attempts=0,
                expires_at=now + timedelta(minutes=CODE_MINUTES),
                created_at=now,
                updated_at=now,
            ),
            code,
        )

    def _replace_challenges(self, challenge: AccountLoginChallenge) -> None:
        """Keep one live challenge per address; a resend revokes the older code."""
        with self._unit_of_work():
            for _ in range(MAX_ATTEMPTS):
                previous = self.challenges.find_by_field("email", challenge.email)
                if previous is None:
                    break
                self.challenges.delete(previous.challenge_id)
            self.challenges.create(challenge)

    def _record_failure(self, challenge: AccountLoginChallenge) -> None:
        """Bound wrong guesses and drop an exhausted challenge."""
        attempts = challenge.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            self.challenges.delete(challenge.challenge_id)
            return
        self.challenges.put(
            AccountLoginChallenge.model_validate(
                challenge.model_copy(
                    update={"attempts": attempts, "updated_at": utc_now()}
                ).model_dump()
            )
        )

    def _verified_identity(self, email: str) -> AccountIdentity | None:
        """Find the one verified email identity, ignoring unverified claims."""
        candidate = self.identities.find_by_field("subject", email)
        if candidate is not None and self._is_verified_email(candidate, email):
            return candidate
        if candidate is None:
            return None
        return next(
            (i for i in self.identities.list() if self._is_verified_email(i, email)),
            None,
        )

    @staticmethod
    def _is_verified_email(identity: AccountIdentity, email: str) -> bool:
        """Only a verified email identity for this exact address can sign in."""
        return (
            identity.kind is AccountIdentityKind.EMAIL
            and identity.subject == email
            and identity.verified_at is not None
        )

    def _find_or_create_account(self, email: str) -> Account:
        """Resolve the platform-wide owner of a verified email, creating it once."""
        identity = self._verified_identity(email)
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
                with self._unit_of_work():
                    self.accounts.create(account)
                    self.identities.create(created)
                return account
            except ValueError:  # lost the race on the verified-identity index
                self._discard(account)
                identity = self._verified_identity(email)
                if identity is None:
                    raise
        return self.accounts.get(identity.account_id)

    def _discard(self, account: Account) -> None:
        """Remove an orphan account when its identity insert was rejected."""
        with suppress(KeyError):
            self.accounts.delete(account.id)
