"""Password, API-token, and user-session authentication use cases."""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import timedelta

from application.ports.repository import Repository
from application.ports.throttling import LoginRateLimiter
from contracts.v1 import (
    ApiCredential,
    ApiCredentialStatus,
    AuditEvent,
    AuditTargetScope,
    Principal,
    PrincipalStatus,
    PrincipalType,
    User,
    UserPasswordCredential,
    UserSession,
    UserSessionStatus,
    UserStatus,
)
from modules.auth.passwords import (
    _DUMMY_PASSWORD_HASH,
    _hash_password,
    _verify_password,
)
from shared.clock import new_id as _id
from shared.clock import utc_now as _auth_now
from shared.clock import utc_now as _now
from shared.errors import LoginThrottled
from shared.login_buckets import login_bucket_key
from shared.request_context import effective_principal_id


class AuthenticationRuntimeHost(ABC):
    """Dependencies required for credential and session authentication."""

    _bootstrapped: Callable[[], bool]
    api_credentials: Repository[ApiCredential]
    audit_events: Repository[AuditEvent]
    login_rate_limiter: LoginRateLimiter
    principals: Repository[Principal]
    user_passwords: Repository[UserPasswordCredential]
    user_sessions: Repository[UserSession]
    users: Repository[User]

    @abstractmethod
    def bootstrap(self) -> None:
        """Ensure persisted users and organization defaults are initialized."""
        raise NotImplementedError

    @abstractmethod
    def authenticate_principal(self, principal_id: str) -> str:
        """Resolve an active principal after checking its current status."""
        raise NotImplementedError

    @abstractmethod
    def _unit_of_work(self) -> AbstractContextManager[None]:
        """Open a transaction for related authentication state changes."""
        raise NotImplementedError


class AuthenticationRuntimeMixin(AuthenticationRuntimeHost):
    """Implement credential verification and revocable user sessions."""

    def authenticate_api_token(self, token: str) -> str | None:
        """Resolve a persisted high-entropy API token to its active principal."""
        if not self._bootstrapped():
            self.bootstrap()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        credential = self.api_credentials.find_by_field("token_digest", digest)
        if credential is None or credential.status is not ApiCredentialStatus.ACTIVE:
            return None
        if credential.expires_at is not None and credential.expires_at <= _now():
            return None
        try:
            return self.authenticate_principal(credential.principal_id)
        except (KeyError, PermissionError):
            return None

    def set_user_password(
        self,
        password: str,
        *,
        current_password: str | None = None,
        requested_by_principal_id: str = "local-developer",
    ) -> None:
        """Set or change the authenticated human user's password."""
        if not self._bootstrapped():
            self.bootstrap()
        if len(password) < 12 or len(password) > 1024 or not password.strip():
            raise ValueError(
                "password must contain 12 to 1024 non-whitespace characters"
            )
        actor_id = effective_principal_id(requested_by_principal_id)
        principal = self.authenticate_principal(actor_id)
        principal_record = self.principals.get(principal)
        if principal_record.principal_type is not PrincipalType.USER:
            raise PermissionError("only human user principals can set a password")
        if principal_record.user_id is None:
            raise PermissionError("user principal is missing its user account")
        user = self.users.get_scoped(principal_record.user_id, principal_record.org_id)
        if user.status is not UserStatus.ACTIVE:
            raise PermissionError("user account is not active")
        try:
            existing = self.user_passwords.get(user.id)
        except KeyError:
            existing = None
        if existing is not None:
            if current_password is None or not _verify_password(
                current_password, existing.password_hash
            ):
                raise ValueError("current password is incorrect")
        elif current_password is not None:
            raise ValueError("a current password is not set")
        timestamp = _now()
        credential = UserPasswordCredential(
            id=user.id,
            org_id=user.org_id,
            password_hash=_hash_password(password),
            changed_by_principal_id=principal,
            created_at=existing.created_at if existing is not None else timestamp,
            updated_at=timestamp,
        )
        active_sessions = self._active_user_sessions(user.org_id, principal)
        self._persist_password_change(credential, active_sessions, user, principal)

    def _active_user_sessions(
        self, org_id: str, principal_id: str
    ) -> tuple[UserSession, ...]:
        return tuple(
            session
            for session in self.user_sessions.list_scoped(org_id)
            if session.principal_id == principal_id
            and session.status is UserSessionStatus.ACTIVE
        )

    def _persist_password_change(
        self,
        credential: UserPasswordCredential,
        active_sessions: tuple[UserSession, ...],
        user: User,
        principal_id: str,
    ) -> None:
        timestamp = credential.updated_at
        with self._unit_of_work():
            self.user_passwords.put(credential)
            for session in active_sessions:
                self.user_sessions.put(
                    UserSession.model_validate(
                        session.model_copy(
                            update={
                                "status": UserSessionStatus.REVOKED,
                                "revoked_at": timestamp,
                                "updated_at": timestamp,
                            }
                        ).model_dump()
                    )
                )
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=user.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=principal_id,
                    action="user.password_updated",
                    target_type="user",
                    target_id=user.id,
                    summary="User password credential updated",
                    request_id=user.id,
                    metadata={"sessions_revoked": len(active_sessions)},
                    created_at=timestamp,
                )
            )

    def authenticate_user_password(
        self,
        email: str,
        password: str,
        *,
        org_id: str | None = None,
        client_address: str | None = None,
    ) -> tuple[User, Principal]:
        """Resolve a user by normalized email and validate its password."""
        if not self._bootstrapped():
            self.bootstrap()
        normalized_email = email.strip().casefold()
        login_buckets = [login_bucket_key("account", normalized_email)]
        if client_address:
            login_buckets.append(login_bucket_key("source", client_address))
        current_time = _auth_now()
        retry_after = 0
        for bucket_key in login_buckets:
            decision = self.login_rate_limiter.consume(bucket_key, current_time)
            if not decision.allowed:
                retry_after = max(retry_after, decision.retry_after_seconds)
        if retry_after:
            raise LoginThrottled(retry_after)
        matches = tuple(
            user
            for user in self.users.list()
            if user.email.casefold() == normalized_email
            and user.status is not UserStatus.DELETED
            and (org_id is None or user.org_id == org_id)
        )
        if len(matches) != 1:
            _verify_password(password, _DUMMY_PASSWORD_HASH)
            raise PermissionError("invalid email or password")
        user = matches[0]
        try:
            credential = self.user_passwords.get(user.id)
        except KeyError:
            _verify_password(password, _DUMMY_PASSWORD_HASH)
            raise PermissionError("invalid email or password") from None
        principal = self.principals.find_by_field("user_id", user.id)
        password_is_valid = _verify_password(password, credential.password_hash)
        if (
            user.status is not UserStatus.ACTIVE
            or principal is None
            or principal.status is not PrincipalStatus.ACTIVE
            or not password_is_valid
        ):
            raise PermissionError("invalid email or password")
        for bucket_key in login_buckets:
            self.login_rate_limiter.clear(bucket_key)
        return user, principal

    def create_user_session(self, principal: Principal) -> tuple[UserSession, str]:
        """Create a short-lived bearer session for an active human principal."""
        self.authenticate_principal(principal.id)
        if principal.principal_type is not PrincipalType.USER:
            raise PermissionError("sessions are only available to human users")
        if principal.user_id is None:
            raise PermissionError("user principal is missing its user account")
        user = self.users.get_scoped(principal.user_id, principal.org_id)
        if user.status is not UserStatus.ACTIVE:
            raise PermissionError("user account is not active")
        token = secrets.token_urlsafe(32)
        timestamp = _now()
        session = UserSession(
            id=_id(),
            org_id=principal.org_id,
            principal_id=principal.id,
            token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            status=UserSessionStatus.ACTIVE,
            expires_at=timestamp + timedelta(hours=12),
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._unit_of_work():
            self.user_sessions.create(session)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=principal.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=principal.id,
                    action="user.session_created",
                    target_type="user_session",
                    target_id=session.id,
                    summary="User signed in",
                    request_id=session.id,
                    created_at=timestamp,
                )
            )
        return session, token

    def authenticate_user_session(self, token: str) -> str | None:
        """Resolve an active, non-expired user session token."""
        if not self._bootstrapped():
            self.bootstrap()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        session = self.user_sessions.find_by_field("token_digest", digest)
        if (
            session is None
            or session.status is not UserSessionStatus.ACTIVE
            or session.expires_at <= _now()
        ):
            return None
        try:
            principal_id = self.authenticate_principal(session.principal_id)
            principal = self.principals.get(principal_id)
            if principal.user_id is None:
                return None
            user = self.users.get_scoped(principal.user_id, principal.org_id)
            if user.status is not UserStatus.ACTIVE:
                return None
            return principal_id
        except (KeyError, PermissionError):
            return None

    def revoke_user_session(
        self, token: str, *, requested_by_principal_id: str
    ) -> None:
        """Revoke the current user's session without affecting API credentials."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        session = self.user_sessions.find_by_field("token_digest", digest)
        if session is None:
            raise KeyError("session not found")
        actor_id = effective_principal_id(requested_by_principal_id)
        if session.principal_id != actor_id:
            raise PermissionError("session belongs to another principal")
        if session.status is UserSessionStatus.REVOKED:
            return
        timestamp = _now()
        revoked = UserSession.model_validate(
            session.model_copy(
                update={
                    "status": UserSessionStatus.REVOKED,
                    "revoked_at": timestamp,
                    "updated_at": timestamp,
                }
            ).model_dump()
        )
        with self._unit_of_work():
            self.user_sessions.put(revoked)
            self.audit_events.create(
                AuditEvent(
                    audit_event_id=_id(),
                    org_id=session.org_id,
                    target_scope=AuditTargetScope.ORGANIZATION,
                    actor_principal_id=actor_id,
                    action="user.session_revoked",
                    target_type="user_session",
                    target_id=session.id,
                    summary="User signed out",
                    request_id=session.id,
                    created_at=timestamp,
                )
            )
