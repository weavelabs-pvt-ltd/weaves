"""Opaque account sessions, kept apart from tenant user sessions."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Literal

from application.ports.repository import Repository
from contracts.v1 import Account, AccountSession, AccountStatus
from shared.clock import new_id, utc_now

ACCOUNT_TOKEN_PREFIX = "wvs_acct_"
ACCOUNT_BROWSER_TOKEN_PREFIX = "wvs_acbr_"
ACCOUNT_SESSION_HOURS = 12
Channel = Literal["bearer", "browser"]
_PREFIXES = {"bearer": ACCOUNT_TOKEN_PREFIX, "browser": ACCOUNT_BROWSER_TOKEN_PREFIX}


def token_digest(token: str) -> str:
    """Return the only form of a session token that is ever stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AccountSessions:
    """Issue, resolve and revoke account sessions."""

    def __init__(
        self, sessions: Repository[AccountSession], accounts: Repository[Account]
    ) -> None:
        """Bind the global session and account repositories."""
        self._sessions = sessions
        self._accounts = accounts

    def issue(
        self, account: Account, channel: Channel = "bearer"
    ) -> tuple[AccountSession, str]:
        """Create a session for an enabled account on one channel.

        Browser tokens carry their own prefix and are valid only as cookies.
        """
        if account.status is not AccountStatus.ACTIVE:
            raise PermissionError("account is disabled")
        token = _PREFIXES[channel] + secrets.token_urlsafe(32)
        now = utc_now()
        session = AccountSession(
            id=new_id(),
            account_id=account.id,
            token_digest=token_digest(token),
            channel=channel,
            expires_at=now + timedelta(hours=ACCOUNT_SESSION_HOURS),
            created_at=now,
            updated_at=now,
        )
        self._sessions.create(session)
        return session, token

    def _active_session(self, token: str, channel: Channel) -> AccountSession:
        """Return the live session for a token presented on its own channel."""
        if not token.startswith(_PREFIXES[channel]):
            raise PermissionError("not an account session token for this channel")
        session = self._sessions.find_by_field("token_digest", token_digest(token))
        if session is None or session.channel != channel or session.revoked_at:
            raise PermissionError("unknown or revoked account session")
        if session.expires_at <= utc_now():
            raise PermissionError("account session expired")
        return session

    def authenticate(self, token: str, channel: Channel = "bearer") -> Account:
        """Resolve a token presented on ``channel`` to its enabled account."""
        session = self._active_session(token, channel)
        try:
            account = self._accounts.get(session.account_id)
        except KeyError as exc:
            raise PermissionError("account no longer exists") from exc
        if account.status is not AccountStatus.ACTIVE:
            raise PermissionError("account is disabled")
        return account

    def revoke(self, token: str, channel: Channel = "bearer") -> None:
        """Revoke the session behind a token; it never authenticates again."""
        session = self._active_session(token, channel)
        now = utc_now()
        self._sessions.put(
            AccountSession.model_validate(
                session.model_copy(
                    update={"revoked_at": now, "updated_at": now}
                ).model_dump()
            )
        )
