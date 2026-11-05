"""Email-code authentication for provisioned organization members."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta

from application.ports.email import EmailSender
from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    AuditEvent,
    AuditTargetScope,
    EmailLoginChallenge,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserSession,
    UserStatus,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _auth_now
from shared.errors import EmailDeliveryUnavailable, LoginThrottled
from shared.login_buckets import login_bucket_key


class EmailLoginRuntimeMixin(ABC):
    """Implement one-time code requests and verification with injected ports."""

    _bootstrapped: Callable[[], bool]
    audit_events: Repository[AuditEvent]
    email_login_challenges: Repository[EmailLoginChallenge]
    email_login_hmac_key: str
    email_sender: EmailSender
    login_rate_limiter: LoginRateLimiter
    principals: Repository[Principal]
    user_sessions: Repository[UserSession]
    users: Repository[User]

    @abstractmethod
    def bootstrap(self) -> None:
        """Ensure user and organization records are initialized."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for identity updates."""
        raise NotImplementedError

    @abstractmethod
    def create_user_session(self, principal: Principal) -> tuple[UserSession, str]:
        """Issue a revocable session for a verified user."""
        raise NotImplementedError

    def start_email_login(
        self,
        email: str,
        *,
        org_id: str | None = None,
        client_address: str | None = None,
    ) -> str:
        """Send a short-lived code only to a pre-provisioned organization user."""
        if not self._bootstrapped():
            self.bootstrap()
        if (
            len(self.email_login_hmac_key.encode("utf-8")) < 32
            or not self.email_sender.configured
        ):
            raise EmailDeliveryUnavailable("Email sign-in is not configured")
        normalized_email = email.strip().casefold()
        self._consume_email_login_limits(normalized_email, client_address)
        matches = self._matching_email_users(normalized_email, org_id)
        if len(matches) != 1:
            # Keep the public response shape identical for unknown addresses.
            # Verification of this opaque, unpersisted handle fails generically.
            return _id()
        user = matches[0]
        principal = self.principals.find_by_field("user_id", user.id)
        if not self._eligible_for_email_login(user, principal):
            return _id()
        challenge, code = self._new_email_login_challenge(user, principal)
        with self._unit_of_work():
            for previous in self.email_login_challenges.list_scoped(user.org_id):
                if previous.principal_id == principal.id:
                    self.email_login_challenges.delete(previous.challenge_id)
            self.email_login_challenges.create(challenge)
        try:
            self.email_sender.send(
                user.email,
                "Your Weaves sign-in code",
                f"Your Weaves sign-in code is {code}. It expires in 10 minutes. "
                "If you did not request it, you can ignore this email.",
            )
        except Exception:
            self.email_login_challenges.delete(challenge.challenge_id)
            raise
        return challenge.challenge_id

    def _consume_email_login_limits(
        self, email: str, client_address: str | None
    ) -> None:
        """Apply shared throttles to code requests by account and source."""
        buckets = [login_bucket_key("email-login-account", email)]
        if client_address:
            buckets.append(login_bucket_key("email-login-source", client_address))
        retry_after = max(
            (
                decision.retry_after_seconds
                for decision in (
                    self.login_rate_limiter.consume(bucket, _auth_now())
                    for bucket in buckets
                )
                if not decision.allowed
            ),
            default=0,
        )
        if retry_after:
            raise LoginThrottled(retry_after)

    def _matching_email_users(self, email: str, org_id: str | None) -> tuple[User, ...]:
        """Resolve an email only when it identifies one eligible tenant account."""
        return tuple(
            user
            for user in self.users.list()
            if user.email.casefold() == email
            and user.status in {UserStatus.ACTIVE, UserStatus.INVITED}
            and (org_id is None or user.org_id == org_id)
        )

    @staticmethod
    def _eligible_for_email_login(user: User, principal: Principal | None) -> bool:
        """Accept active users and legacy accounts awaiting first sign-in."""
        if principal is None or principal.principal_type is not PrincipalType.USER:
            return False
        return principal.status is PrincipalStatus.ACTIVE or (
            user.status is UserStatus.INVITED
            and principal.status is PrincipalStatus.DISABLED
        )

    def _new_email_login_challenge(
        self, user: User, principal: Principal
    ) -> tuple[EmailLoginChallenge, str]:
        """Create an expiring code whose digest is protected by the server key."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge_id = _id()
        timestamp = _auth_now()
        challenge = EmailLoginChallenge(
            challenge_id=challenge_id,
            org_id=user.org_id,
            user_id=user.id,
            principal_id=principal.id,
            email=user.email,
            code_digest=self._email_code_digest(challenge_id, code),
            attempts=0,
            expires_at=timestamp + timedelta(minutes=10),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return challenge, code

    def _email_code_digest(self, challenge_id: str, code: str) -> str:
        """Bind the short code to a challenge using a server-held HMAC key."""
        message = f"{challenge_id}:{code}".encode()
        return hmac.new(
            self.email_login_hmac_key.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()

    def verify_email_login(
        self, challenge_id: str, code: str, *, client_address: str | None = None
    ) -> tuple[User, Principal, UserSession, str]:
        """Consume a valid email code, activate legacy members, and start a session."""
        if not self._bootstrapped():
            self.bootstrap()
        try:
            challenge = self.email_login_challenges.get(challenge_id)
        except KeyError as exc:
            raise PermissionError("email code is invalid or expired") from exc
        self._consume_email_login_limits(challenge.email, client_address)
        if challenge.expires_at <= _auth_now() or challenge.attempts >= 5:
            self.email_login_challenges.delete(challenge_id)
            raise PermissionError("email code is invalid or expired")
        if not hmac.compare_digest(
            challenge.code_digest, self._email_code_digest(challenge_id, code)
        ):
            self._record_email_login_failure(challenge)
            raise PermissionError("email code is invalid or expired")
        try:
            self.email_login_challenges.consume(challenge_id)
        except KeyError as exc:
            raise PermissionError("email code is invalid or expired") from exc
        user, principal = self._activate_email_login_account(challenge)
        session, token = self.create_user_session(principal)
        self.login_rate_limiter.clear(
            login_bucket_key("email-login-account", challenge.email)
        )
        return user, principal, session, token

    def _record_email_login_failure(self, challenge: EmailLoginChallenge) -> None:
        """Bound incorrect code attempts and remove exhausted challenges."""
        attempts = challenge.attempts + 1
        if attempts >= 5:
            self.email_login_challenges.delete(challenge.challenge_id)
            return
        updated = EmailLoginChallenge.model_validate(
            challenge.model_copy(
                update={"attempts": attempts, "updated_at": _auth_now()}
            ).model_dump()
        )
        self.email_login_challenges.put(updated)

    def _activate_email_login_account(
        self, challenge: EmailLoginChallenge
    ) -> tuple[User, Principal]:
        """Recheck provisioned membership and activate a legacy pending account."""
        user = self.users.get_scoped(challenge.user_id, challenge.org_id)
        principal = self.principals.get_scoped(challenge.principal_id, challenge.org_id)
        email_matches = user.email.casefold() == challenge.email
        if not email_matches or not self._eligible_for_email_login(user, principal):
            raise PermissionError("email code is invalid or expired")
        if user.status is UserStatus.INVITED:
            timestamp = _auth_now()
            user = User.model_validate(
                user.model_copy(
                    update={"status": UserStatus.ACTIVE, "updated_at": timestamp}
                ).model_dump()
            )
            principal = Principal.model_validate(
                principal.model_copy(
                    update={"status": PrincipalStatus.ACTIVE, "updated_at": timestamp}
                ).model_dump()
            )
            with self._unit_of_work():
                self.users.put(user)
                self.principals.put(principal)
                self.audit_events.create(
                    AuditEvent(
                        audit_event_id=_id(),
                        org_id=user.org_id,
                        target_scope=AuditTargetScope.ORGANIZATION,
                        actor_principal_id=principal.id,
                        action="user.activated_by_email",
                        target_type="user",
                        target_id=user.id,
                        summary="Provisioned user verified their email address",
                        request_id=challenge.challenge_id,
                        created_at=timestamp,
                    )
                )
        return user, principal
