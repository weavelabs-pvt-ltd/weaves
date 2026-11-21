"""Build account challenge and session rows with chosen expiry for hygiene tests."""

from __future__ import annotations

from datetime import datetime

from contracts.v1 import AccountLoginChallenge, AccountSession
from shared.clock import new_id, utc_now


def challenge_row(email: str, expires_at: datetime) -> AccountLoginChallenge:
    """One challenge for an address that expires at the given moment."""
    now = utc_now()
    return AccountLoginChallenge(
        challenge_id=new_id(),
        email=email,
        code_digest="a" * 64,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )


def session_row(
    account_id: str, expires_at: datetime, revoked_at: datetime | None = None
) -> AccountSession:
    """One session for an account, optionally already revoked."""
    now = utc_now()
    return AccountSession(
        id=new_id(),
        account_id=account_id,
        token_digest=new_id().replace("-", "").ljust(64, "0")[:64],
        expires_at=expires_at,
        revoked_at=revoked_at,
        created_at=now,
        updated_at=now,
    )
